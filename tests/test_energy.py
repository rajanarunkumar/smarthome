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


def add_activity(s, device_id, name, kind, day, start, end):
    s.db.execute("INSERT INTO device_activity (device_id, name, kind, start, end) VALUES (?,?,?,?,?)",
                 (device_id, name, kind, f"{day}T{start}", f"{day}T{end}"))


def test_lights_are_not_given_made_up_power():
    """An LED office dimmer on 9-5 every weekday overlaps the AC's daytime load; it must not be blamed for it."""
    s = store()
    tmean = build(s)
    for n in range(1, DAYS + 1):
        add_activity(s, "leviton:office", "Office", "dimmer", (TODAY - timedelta(days=n)).isoformat(), "09:00", "17:00")
    s.db.commit()
    out = build_energy(s, tmean, days=DAYS, today=TODAY)
    office = next(d for d in out["devices"] if d["id"] == "leviton:office")
    assert office["kw"] is None and office["kwh"] is None and office["status"] == "small"
    assert any(r["title"].startswith("Lights and small devices") for r in out["recommendations"])
    assert not any("Office" in r["text"] for r in out["recommendations"] if r["title"] == "Still learning some devices")


def test_entered_watts_count_and_dont_disturb_the_others():
    s = store()
    tmean = build(s)
    intervals = s.intervals(TODAY - timedelta(days=40))
    for n in range(1, DAYS + 1):                       # two incandescent chandeliers, 600 W together, 8-11 PM
        d = (TODAY - timedelta(days=n)).isoformat()
        add_activity(s, "leviton:chand", "Dining chandelier", "dimmer", d, "20:00", "23:00")
        for h in (20, 21, 22):
            for m in (0, 15, 30, 45):
                intervals[f"{d}T{h:02d}:{m:02d}"] += 0.6 / 4
    s.put_intervals(intervals.items())
    s.set_watts("leviton:chand", 600)
    out = build_energy(s, tmean, days=DAYS, today=TODAY)
    chand = next(d for d in out["devices"] if d["id"] == "leviton:chand")
    assert chand["status"] == "entered" and chand["kw"] == 0.6 and chand["kwh"] == round(0.6 * 3 * DAYS, 1)
    power = device_power(s.intervals(TODAY - timedelta(days=40)), s.activity(TODAY - timedelta(days=40)), known_watts=s.watts())
    assert power["thinq:d1"]["kw"] == 5.0 and power["pentair:p1"]["kw"] == 1.5
    s.set_watts("leviton:chand", None)
    assert s.watts() == {}


def test_a_plug_with_no_real_load_gets_no_estimate():
    import random
    rng = random.Random(7)
    s = store()
    tmean = build(s)
    intervals = s.intervals(TODAY - timedelta(days=40))
    for k in intervals:                                  # realistic noise: ±0.4 kW swings
        intervals[k] = max(intervals[k] + rng.uniform(-0.1, 0.1), 0)
    s.put_intervals(intervals.items())
    for n in range(1, DAYS + 1):
        d = (TODAY - timedelta(days=n)).isoformat()
        h = rng.randint(6, 21)
        add_activity(s, "kasa:phone", "Phone charger", "plug", d, f"{h:02d}:00", f"{h + 1:02d}:00")
    s.db.commit()
    power = device_power(s.intervals(TODAY - timedelta(days=40)), s.activity(TODAY - timedelta(days=40)))
    assert "kasa:phone" not in power
    assert abs(power["thinq:d1"]["kw"] - 5.0) < 0.2


def test_bulbs_get_power_from_their_model_and_brightness():
    s = store()
    lamp = Device(id="hue:1", brand="hue", name="Chandelier", kind="light", on=True)
    color = Device(id="hue:2", brand="hue", name="Desk", kind="light", model="Hue color lamp", model_id="LCA001", on=True, brightness=100)
    dim = Device(id="hue:3", brand="hue", name="Hall", kind="light", model_id="LCA001", on=True, brightness=50)
    govee = Device(id="govee:4", brand="govee", name="Porch", kind="light", model="H6008", on=True)
    strip = Device(id="govee:5", brand="govee", name="TV strip", kind="light", model="H9999", on=True)
    s.set_watts("hue:1", 300)                                   # entered: not estimated
    t0 = datetime(2026, 11, 20, 20, 0)
    for i in range(3):
        s.observe([lamp, color, dim, govee, strip], t0 + timedelta(minutes=2 * i))
    s.observe([], t0 + timedelta(minutes=30))
    assert {a["device_id"] for a in s.activity(date(2026, 11, 19))} == {"hue:1", "hue:2", "hue:3", "govee:4", "govee:5"}
    est = s.estimated()
    assert "hue:1" not in est
    assert est["hue:2"]["watts"] == 6.8 and est["hue:2"]["source"] == "measured" and "A19" in est["hue:2"]["profile"]
    assert 2.0 < est["hue:3"]["watts"] < 2.4                     # half brightness draws about a third
    assert est["govee:4"]["watts"] == 9.0 and est["govee:4"]["source"] == "rated"
    assert est["govee:5"]["source"] == "typical"
    assert s.known_watts()["hue:1"] == 300 and s.known_watts()["hue:2"] == 6.8


def test_light_profiles():
    from hub.lights import profile, watts_at
    p = profile(None, "KL135(US)")
    assert p["source"] == "measured" and watts_at(p, None) == p["curve"][10] and watts_at(p, 5) == round((p["curve"][0] + p["curve"][1]) / 2, 2)
    assert profile(None, "Hue lightstrip plus")["name"] == "typical LED strip"


def test_watts_reported_by_a_device_are_used_instead_of_guessing():
    s = store()
    tmean = build(s)
    heater = lambda w, on=True: Device(id="kasa:heater", brand="kasa", name="Space heater", kind="plug", on=on, watts=w)  # noqa: E731
    t = datetime(2026, 11, 19, 7, 0)
    for w in (1490, 1510, 1500, 1.0):          # the last is the plug's own standby while "on": ignored
        s.observe([heater(w)], t)
        t += timedelta(minutes=2)
    s.observe([heater(0, on=False)], t)
    assert s.reported() == {"kasa:heater": 1500.0}
    s.set_watts("thinq:d1", 4800)               # the owner's number wins over anything else
    assert s.known_watts() == {"kasa:heater": 1500.0, "thinq:d1": 4800}
    out = build_energy(s, tmean, days=DAYS, today=TODAY)
    heater_row = next(d for d in out["devices"] if d["id"] == "kasa:heater")
    dryer = next(d for d in out["devices"] if d["id"] == "thinq:d1")
    assert heater_row["status"] == "reported" and heater_row["kw"] == 1.5 and heater_row["reported_w"] == 1500
    assert dryer["status"] == "entered" and dryer["kw"] == 4.8


def test_kasa_plugs_with_energy_monitoring_report_watts():
    from kasa import Module

    from hub.adapters.kasa_adapter import KasaAdapter
    energy = SimpleNamespace(current_consumption=61.24, consumption_today=0.83)
    plug = SimpleNamespace(alias="TV corner", model="KP125M", is_on=True, modules={Module.Energy: energy})
    plain = SimpleNamespace(alias="Lamp", model="HS103", is_on=True, modules={})
    a = KasaAdapter()
    a._is_dimmer = lambda dev: False
    with_energy, without = a._to_device("kasa:1", plug), a._to_device("kasa:2", plain)
    assert with_energy.watts == 61.2 and {"label": "Today", "value": "0.83 kWh"} in with_energy.details
    assert without.watts is None and without.details == []


def test_always_on_appliances_come_out_of_everything_else():
    s = store()
    tmean = build(s)
    before = build_energy(s, tmean, days=DAYS, today=TODAY)
    s.add_fixed_load("Garage freezer", "freezer", None)                 # typical 534 kWh/yr
    s.add_fixed_load("Sub-Zero", "fridge", 637)                          # from the label
    beer = s.add_fixed_load("Beer cooler", "beverage", None)
    s.add_fixed_load("Bar cooler", "beverage", None)
    out = build_energy(s, tmean, days=DAYS, today=TODAY)
    per_day = (534 + 637 + 300 + 300) / 365
    assert all(abs(r["appliances_kwh"] - round(per_day, 1)) < 0.05 for r in out["series"])
    assert [f["source"] for f in out["fixed_loads"]] == ["typical", "label", "typical", "typical"]
    b, a = before["series"][0], out["series"][0]
    assert a["total_kwh"] == b["total_kwh"] and a["other_kwh"] + (a["hvac_kwh"] or 0) < b["other_kwh"] + (b["hvac_kwh"] or 0)
    rec = next(r for r in out["recommendations"] if r["title"].startswith("Fridges and freezers"))
    assert "1,771 kWh a year" in rec["title"] and "202 W" in rec["text"] and "Garage freezer" in rec["text"] and "Sub-Zero" not in rec["text"]
    s.update_fixed_load(beer, "Beer cooler", "beverage", 410)
    s.delete_fixed_load(beer)
    assert len(s.fixed_loads()) == 3


def test_appliance_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        r = client.post("/api/energy/appliances", json={"name": "GE upright freezer", "type": "freezer"}, headers=token)
        load_id = r.json()["id"]
        assert client.post("/api/energy/appliances", json={"name": "x", "type": "jacuzzi"}, headers=token).status_code == 422
        client.put(f"/api/energy/appliances/{load_id}", json={"name": "GE upright freezer", "type": "freezer", "kwh_year": 534}, headers=token)
        assert hub.energy.fixed_loads()[0]["source"] == "label"
        client.delete(f"/api/energy/appliances/{load_id}", headers=token)
        assert hub.energy.fixed_loads() == []


def test_quiet_weekday_afternoons_give_the_always_on_load():
    """300 W of network/standby gear plus two fridges cycling (150 W each, on 40% of the time = 120 W on average):
    the true always-on load averages 420 W. Weekends add cooking and laundry at midday; the AC runs (and is
    recorded) on hot weekday afternoons; the pool pump runs 12-2 PM on some days and reports its 1,200 W."""
    from hub.energy import quiet_baseline
    import random
    rng = random.Random(5)
    s = store()
    intervals = {}
    start = date(2026, 10, 26)                                   # four weeks, Monday start
    for n in range(28):
        d = start + timedelta(days=n)
        hot = n % 3 == 0
        for slot in range(96):
            t = datetime.combine(d, dtime()) + timedelta(minutes=15 * slot)
            w = 300 + sum(150 for _ in range(2) if rng.random() < 0.4)
            if d.weekday() >= 5 and 11 <= t.hour < 15:
                w += 2000                                        # weekend cooking, laundry
            if hot and d.weekday() < 5 and 13 <= t.hour < 16:
                w += 3500                                        # AC
            if n % 4 == 0 and 12 <= t.hour < 14:
                w += 1200                                        # pool pump
            intervals[t.strftime("%Y-%m-%dT%H:%M")] = w / 4000
        if hot and d.weekday() < 5:
            s.db.execute("INSERT INTO device_activity (device_id, name, kind, start, end) VALUES (?,?,?,?,?)",
                         ("lennox:1", "Thermostat", "thermostat", f"{d}T13:00", f"{d}T16:00"))
        if n % 4 == 0:
            s.db.execute("INSERT INTO device_activity (device_id, name, kind, start, end) VALUES (?,?,?,?,?)",
                         ("pentair:p1", "Pool pump", "pool", f"{d}T12:00", f"{d}T14:00"))
    s.db.execute("INSERT INTO device_activity (device_id, name, kind, start, end) VALUES (?,?,?,?,?)",
                 ("lennox:1", "Thermostat", "thermostat", f"{start - timedelta(days=1)}T13:00", f"{start - timedelta(days=1)}T13:30"))
    s.db.commit()
    s.put_intervals(intervals.items())
    activity = s.activity(start - timedelta(days=2))
    base = quiet_baseline(s.intervals(start), activity, {"pentair:p1": 1200})
    assert abs(base["watts"] - 420) <= 30 and "idle" in base["method"]
    assert always_on_watts(s.intervals(start)) <= 330             # the overnight minimum catches the fridges off

    # Listed fridges whose typical figures add up to more than the whole always-on load are called out.
    for name in ("Freezer", "Fridge", "Cooler 1", "Cooler 2", "Cooler 3", "Cooler 4", "Cooler 5", "Cooler 6"):   # 8 x 61 W
        s.add_fixed_load(name, "freezer", None)
    out = build_energy(s, {}, days=28, today=start + timedelta(days=28))
    rec = next(r for r in out["recommendations"] if r["title"].startswith("Always-on load"))
    assert out["baseline"]["watts"] == base["watts"] or abs(out["baseline"]["watts"] - 420) <= 30
    assert "nearly all of it" in rec["text"] and "-" not in rec["text"].split("account")[0][-5:]   # 488 W vs ~450 W
    for name in ("Cooler 7", "Cooler 8"):
        s.add_fixed_load(name, "freezer", None)                    # now 610 W listed
    out = build_energy(s, {}, days=28, today=start + timedelta(days=28))
    rec = next(r for r in out["recommendations"] if r["title"].startswith("Always-on load"))
    assert "more than the whole always-on load" in rec["text"]


def test_without_thermostat_history_only_mild_days_count():
    from hub.energy import quiet_baseline
    intervals, tmean = {}, {}
    start = date(2026, 10, 26)
    for n in range(28):
        d = start + timedelta(days=n)
        tmean[d.isoformat()] = 64 if n % 2 else 88               # every other day hot, AC unrecorded
        for slot in range(96):
            t = datetime.combine(d, dtime()) + timedelta(minutes=15 * slot)
            w = 400 + (3000 if n % 2 == 0 and 12 <= t.hour < 16 else 0)
            intervals[t.strftime("%Y-%m-%dT%H:%M")] = w / 4000
    assert quiet_baseline(intervals, [], {}, tmean)["watts"] == 400
    assert quiet_baseline(intervals, [], {}, {}) is not None    # no weather either: falls back to the lower quarter
