"""Electricity analytics and the 4-week backfill helpers, on synthetic data with known answers."""

import sqlite3
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace

from hub.adapters.rainbird_adapter import scheduled_runs
from hub.adapters.smt_adapter import parse_intervals
from hub.adapters.thinq_adapter import cycle_running
from hub.energy import (EnergyStore, always_on_watts, build_energy, device_power, hvac_model, spikes)
from hub.history import confirm_with_meter
from hub.models import Device

TODAY = date(2026, 11, 21)
DAYS = 28


def temp(d: date) -> float:
    return 60 + (d.toordinal() % 9) * 3  # 60–84 °F, varies day to day


def build(store: EnergyStore):
    """Always-on 0.4 kW; AC adds 0.06 kW per °F above 65 from 10 AM–10 PM; dryer 5 kW for 60 min
    every 3rd day at 7 PM; pool pump 1.5 kW 9–11 AM on even days."""
    intervals = {}
    for n in range(1, DAYS + 1):
        d = TODAY - timedelta(days=n)
        ac_kw = max(temp(d) - 65, 0) * 0.06
        for slot in range(96):
            t = datetime.combine(d, dtime()) + timedelta(minutes=15 * slot)
            kw = 0.4 + (ac_kw if 10 <= t.hour < 22 else 0)
            if n % 3 == 0 and t.hour == 19:
                kw += 5.0
            if n % 2 == 0 and 9 <= t.hour < 11:
                kw += 1.5
            intervals[t.strftime("%Y-%m-%dT%H:%M")] = kw / 4
        if n % 3 == 0:
            store.db.execute("INSERT INTO device_activity (device_id, name, kind, start, end) VALUES (?,?,?,?,?)",
                             ("thinq:d1", "Dryer", "appliance", f"{d}T19:00", f"{d}T20:00"))
        if n % 2 == 0:
            store.db.execute("INSERT INTO device_activity (device_id, name, kind, start, end) VALUES (?,?,?,?,?)",
                             ("pentair:p1", "Pool pump", "pool", f"{d}T09:00", f"{d}T11:00"))
    store.db.commit()
    store.put_intervals(intervals.items())
    return {(TODAY - timedelta(days=n)).isoformat(): temp(TODAY - timedelta(days=n)) for n in range(1, DAYS + 1)}


def store():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return EnergyStore(db)


def test_device_power_recovers_each_device():
    s = store()
    build(s)
    since = TODAY - timedelta(days=DAYS + 1)
    power = device_power(s.intervals(since), s.activity(since))
    assert power["thinq:d1"]["kw"] == 5.0
    assert power["pentair:p1"]["kw"] == 1.5


def test_hvac_model_finds_cooling_slope_and_base():
    s = store()
    tmean = build(s)
    daily = {}
    for slot, kwh in s.intervals(TODAY - timedelta(days=40)).items():
        daily[slot[:10]] = daily.get(slot[:10], 0) + kwh
    # Remove the known devices, as build_energy does, then fit.
    for n in range(1, DAYS + 1):
        d = (TODAY - timedelta(days=n)).isoformat()
        daily[d] -= (5.0 if n % 3 == 0 else 0) + (3.0 if n % 2 == 0 else 0)
    model = hvac_model(daily, tmean)
    assert model["base_kwh"] == 9.6        # 0.4 kW × 24 h
    assert model["per_cdd"] == 0.72         # 0.06 kW × 12 h per °F
    assert model["r2"] == 1.0


def test_always_on_and_spikes():
    s = store()
    build(s)
    since = TODAY - timedelta(days=DAYS + 1)
    assert always_on_watts(s.intervals(since)) == 400
    found = spikes(s.intervals(since), s.activity(since), {"thinq:d1": "Dryer", "pentair:p1": "Pool pump"})
    assert found and all(e["running"] == ["Dryer"] for e in found)
    assert found[0]["peak_kw"] >= 5.4 and found[0]["kwh"] >= 5.0


def test_build_energy_end_to_end():
    s = store()
    tmean = build(s)
    out = build_energy(s, tmean, days=DAYS, today=TODAY)
    assert out["has_energy"] and out["has_activity"] and out["standby_w"] == 400
    dryer = next(d for d in out["devices"] if d["name"] == "Dryer")
    assert (dryer["kw"], dryer["kwh"], dryer["hours"]) == (5.0, 45.0, 9.0)  # 9 loads × 1 h × 5 kW
    assert out["hvac"]["per_cdd"] == 0.72
    titles = [r["title"] for r in out["recommendations"]]
    assert any(t.startswith("Heating and cooling: about") for t in titles)
    assert "Always-on load: about 400 W" in titles


def test_activity_recorder_opens_and_closes_periods():
    s = store()
    t0 = datetime(2026, 11, 20, 19, 0)
    dryer = lambda on: Device(id="thinq:d1", brand="thinq", name="Dryer", kind="appliance", on=on)
    lamp = Device(id="hue:1", brand="hue", name="Lamp", kind="light", on=True)  # lights aren't tracked
    s.observe([dryer(True), lamp], t0)
    s.observe([dryer(True), lamp], t0 + timedelta(minutes=30))
    s.observe([dryer(False), lamp], t0 + timedelta(minutes=58))
    assert s.activity(date(2026, 11, 1)) == [{"device_id": "thinq:d1", "name": "Dryer", "kind": "appliance",
                                              "start": "2026-11-20T19:00", "end": "2026-11-20T19:58"}]


def test_laundry_running_state():
    assert cycle_running({"runState": {"currentState": "DRYING"}}) is True
    assert cycle_running({"runState": {"currentState": "END"}}) is False
    assert cycle_running({}) is None
    assert cycle_running([{"runState": {"currentState": "POWER_OFF"}}, {"runState": {"currentState": "RUNNING"}}]) is True


def test_smart_meter_texas_slots():
    assert parse_intervals([["11/20/2026 0:00", "0.25"], ["11/20/2026 13:45", "1.5"], ["bad", "x"]]) == [
        ("2026-11-20T00:00", 0.25), ("2026-11-20T13:45", 1.5)]


def program(days=("MONDAY", "WEDNESDAY"), starts=(dtime(5, 0),), zones=((1, 20), (2, 10))):
    return SimpleNamespace(frequency=SimpleNamespace(name="CUSTOM"), days_of_week=[SimpleNamespace(name=d) for d in days],
                           starts=list(starts), durations=[SimpleNamespace(zone=z, duration=timedelta(minutes=m)) for z, m in zones])


def test_scheduled_runs_replay_programs_back_to_back():
    schedule = SimpleNamespace(programs=[program()], zone_schedules={})
    runs = scheduled_runs(schedule, date(2026, 11, 16), date(2026, 11, 19), adjust_pct=50)  # Mon 16 – Thu 19
    assert runs == [
        {"started": "2026-11-16T05:00", "zone": 1, "minutes": 10.0}, {"started": "2026-11-16T05:10", "zone": 2, "minutes": 5.0},
        {"started": "2026-11-18T05:00", "zone": 1, "minutes": 10.0}, {"started": "2026-11-18T05:10", "zone": 2, "minutes": 5.0},
    ]


def test_reconstructed_runs_need_the_meter_to_agree():
    runs = [{"started": "2026-11-16T05:00", "zone": 1, "minutes": 20}, {"started": "2026-11-18T05:00", "zone": 1, "minutes": 20}]
    hourly = {f"2026-11-{d:02d}T{h:02d}": 3.0 for d in range(14, 20) for h in range(24)}
    hourly["2026-11-16T05"] += 180  # the 16th really watered; the 18th was skipped (rain delay)
    assert confirm_with_meter(runs, hourly) == [runs[0]]
