import sqlite3
from datetime import datetime, timedelta

from hub.pool import (PoolStore, build_pool, diagnose, energy_by_day, flow_efficiency, minutes_outside, parse_programs,
                      parse_reading)
from hub.tou import tou_report

START = datetime(2026, 11, 1, 20, 0)
WINDOW = (20, 4)


def sample(t, watts, gpm=60.0, target=60.0, speed=70.0, running=True, pressure=20.0):
    return {"t": t.isoformat(timespec="seconds"), "gpm": gpm, "watts": watts, "speed_pct": speed, "pressure_psi": pressure,
            "target": target, "target_kind": "gpm", "program": 5, "running": int(running), "alarm": 0}


def nightly(days, watts_for_day, start=START, hours=5, target=60.0):
    """A 5-hour run every night from 8 PM, a reading every 2 minutes."""
    rows = []
    for d in range(days):
        t0 = start + timedelta(days=d)
        for i in range(hours * 30):
            rows.append(sample(t0 + timedelta(minutes=2 * i), watts_for_day(d), target=target, gpm=target))
        rows.append(sample(t0 + timedelta(minutes=2 * hours * 30), 0, gpm=0, running=False))
    return rows


def test_parse_reading_units():
    f = {k: {"value": v} for k, v in {"s26": "701", "s18": "1312", "s19": "805", "s17": "2269", "s15": "2",
                                       "s16": "700", "s14": "0", "s20": "0", "s1": "261122110700"}.items()}
    r = parse_reading(f)
    assert r == {"gpm": 70.1, "watts": 1312.0, "speed_pct": 80.5, "pressure_psi": 22.69, "target": 70.0, "target_kind": "gpm",
                 "program": 0, "running": True, "alarm": 0, "device_time": "261122110700"}


def test_parse_programs_and_paid_minutes():
    f = {}
    for n, (name, val, start, end, en) in {5: ("8 to 4", "600", 1200, 60, "1"), 8: ("10 to 8", "300", 670, 1200, "1")}.items():
        for e, v in {2: name, 3: "2", 4: val, 5: "0", 6: start, 7: end, 8: 127, 10: en, 13: "1"}.items():
            f[f"zp{n}e{e}"] = {"value": str(v)}
    progs = {p["name"]: p for p in parse_programs(f)}
    assert progs["10 to 8"]["target"] == 30.0
    assert minutes_outside(1200, 60, WINDOW) == 0          # 8 PM to 1 AM: all free
    assert minutes_outside(670, 1200, WINDOW) == 530       # 11:10 AM to 8 PM: all paid
    assert minutes_outside(180, 300, WINDOW) == 60         # 3 to 5 AM: the hour after 4 is paid


def test_clean_filter_is_good_and_energy_is_free():
    rows = nightly(5, lambda d: 800)
    h = diagnose(rows, [], START + timedelta(days=4, hours=6))
    assert h["level"] == "good" and h["ratio"] == 1.0
    e = energy_by_day(rows, WINDOW)
    assert all(d["paid_kwh"] == 0 for d in e)
    assert abs(sum(d["free_kwh"] for d in e) - 5 * 4.0) < 0.1   # 800 W × 5 h × 5 nights


def test_gradual_rise_points_to_backwash():
    rows = nightly(8, lambda d: 800 * (1 + 0.04 * d))      # +4% a night
    h = diagnose(rows, [], START + timedelta(days=7, hours=6))
    assert h["level"] in ("warning", "serious")
    assert h["actions"][0]["action"] == "backwash"
    assert any("steadily" in e for e in h["evidence"])


def test_sudden_jump_points_to_skimmer():
    rows = nightly(6, lambda d: 800 if d < 5 else 1000)
    h = diagnose(rows, [], START + timedelta(days=5, hours=6))
    assert h["actions"][0]["action"] == "skimmer"
    assert any("jumped" in e for e in h["evidence"])


def test_backwash_that_did_not_help_points_to_filter_clean():
    rows = nightly(8, lambda d: 800 * (1 + 0.04 * d))
    backwash = {"id": 1, "at": (START + timedelta(days=5, hours=6)).isoformat(timespec="minutes"), "action": "backwash", "note": None}
    h = diagnose(rows, [backwash], START + timedelta(days=7, hours=6))
    assert h["ratio"] > 1.2                                  # the backwash didn't become the new baseline
    assert h["actions"][0]["action"] == "filter_clean"


def test_cannot_hold_flow_means_water_level_first():
    rows = nightly(3, lambda d: 800)
    t = START + timedelta(days=3)
    rows += [sample(t + timedelta(minutes=2 * i), 1400, gpm=50, target=60, speed=99) for i in range(10)]
    h = diagnose(rows, [], t + timedelta(minutes=25))
    assert h["level"] == "serious"
    assert h["actions"][0]["action"] == "water_fill"


def test_store_and_maintenance_effect():
    db = sqlite3.connect(":memory:")
    store = PoolStore(db)
    rows = nightly(4, lambda d: 800 if d < 2 else 1000)
    rows += nightly(2, lambda d: 800, start=START + timedelta(days=4))
    for r in rows:
        store.record({**r, "device_time": r["t"]}, datetime.fromisoformat(r["t"]))
    fix_at = START + timedelta(days=3, hours=10)
    store.log("skimmer", fix_at)
    out = build_pool(store, [], None, {"free_start": 20, "free_end": 4, "delivery_cents": 5.6, "energy_cents": 14.0},
                     START + timedelta(days=5, hours=6), days=14)
    eff = out["maintenance"][0]["effect"]
    assert eff and eff["change_pct"] < -15                   # 1000 W -> 800 W
    assert out["cost"]["paid"] == 0 and out["cost"]["free"] > 0


def test_repeated_device_time_is_skipped():
    store = PoolStore(sqlite3.connect(":memory:"))
    r = {**sample(START, 800), "device_time": "x"}
    assert store.record(r, START) is True
    assert store.record(r, START + timedelta(minutes=2)) is False


def test_flow_efficiency_lower_flow_cheaper():
    rows = nightly(1, lambda d: 1300, target=70) + nightly(1, lambda d: 300, target=40, start=START + timedelta(days=1))
    eff = flow_efficiency(rows)
    assert [e["gpm"] for e in eff] == [40, 70]
    assert eff[0]["gal_per_kwh"] > eff[1]["gal_per_kwh"] * 2


def test_tou_report_splits_free_and_paid():
    iv = {}
    for h in range(24):
        for q in range(4):
            iv[f"2026-11-01T{h:02d}:{q * 15:02d}"] = 1.0 if h in (17, 18, 19) else 0.25
    r = tou_report(iv, {"free_start": 20, "free_end": 4, "energy_cents": 15.0, "delivery_cents": 5.0, "monthly_fee": 4.0})
    assert r["free_kwh"] == 8.0 and r["paid_kwh"] == 25.0
    assert r["cost"]["energy"] == round(25 * 0.15, 2)
    assert any("could wait" in x["title"] for x in r["recommendations"])


def test_stale_reading_is_not_live_and_repeat_is_skipped():
    from hub.pool import age_minutes
    store = PoolStore(sqlite3.connect(":memory:"))
    live = {**sample(START, 1300), "device_time": START.strftime("%y%m%d%H%M%S")}
    assert age_minutes(live, START + timedelta(minutes=55)) == 55.0
    noon = START.replace(hour=12)
    live_noon = {**sample(noon, 1300), "device_time": noon.strftime("%y%m%d%H%M%S")}
    out = build_pool(store, [], live_noon, {}, noon + timedelta(minutes=55), days=7)
    assert out["live"]["stale"] is True
    assert not any("Running now" in f["title"] for f in out["findings"])
    assert store.record(live, START + timedelta(minutes=2)) is True
    store._last_device_time = None                       # as after a restart
    assert store.record(live, START + timedelta(minutes=4)) is False
