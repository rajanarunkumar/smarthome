import sqlite3
from datetime import date, datetime, timedelta

from hub.hvac import HvacStore, build_hvac, daily, diagnose, fighting, runs

WINDOW = (20, 4)
START = datetime(2026, 7, 1)
TARIFF = {"free_start": 20, "free_end": 4, "energy_cents": 22.8, "delivery_cents": 7.1, "night_delivery_cents": 7.1, "fees_pct": 4}


def day_samples(day: datetime, on_minutes, dev="lennox:1", hvac="COOLING", indoor=76, cool=75, humidity=50):
    """A full day of 2-minute samples; on_minutes(minute_of_day) -> True while the system runs."""
    rows = []
    for m in range(0, 1440, 2):
        t = day + timedelta(minutes=m)
        rows.append({"device_id": dev, "t": t.isoformat(timespec="seconds"), "name": "Upstairs",
                     "hvac": hvac if on_minutes(m) else "OFF", "mode": "COOL", "indoor_f": indoor, "humidity": humidity,
                     "heat_f": 68, "cool_f": cool})
    return rows


def cycles(per_hour: int, length: int):
    """`per_hour` runs of `length` minutes each hour."""
    gap = 60 // per_hour
    return lambda m: (m % 60) % gap < length


def store_with(rows):
    store = HvacStore(sqlite3.connect(":memory:"))
    for r in rows:
        store.db.execute("INSERT INTO hvac_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (r["device_id"], r["t"], r["name"], r["hvac"], r["mode"], r["indoor_f"], r["humidity"], r["heat_f"], r["cool_f"]))
    return store


def test_runs_split_free_and_paid():
    rows = day_samples(START, lambda m: 19 * 60 <= m < 21 * 60)        # 7 to 9 PM
    r = runs(rows, WINDOW)
    assert len(r) == 1 and r[0]["minutes"] == 120
    assert r[0]["paid_min"] == 60 and r[0]["free_min"] == 60
    assert not r[0]["clipped"]


def test_gap_clips_a_run():
    rows = [x for x in day_samples(START, lambda m: 600 <= m < 700) if not 640 <= int(x["t"][11:13]) * 60 + int(x["t"][14:16]) < 660]
    r = runs(rows, WINDOW)
    assert len(r) == 2 and all(x["clipped"] for x in r)


def test_clean_system_is_good_and_rising_runtime_points_to_filter():
    tmean = {}
    rows = []
    for d in range(10):
        day = START + timedelta(days=d)
        tmean[day.date().isoformat()] = 85.0                                   # 20 cooling degree-days
        minutes = 12 if d < 7 else 18                                          # 50% longer runs for the same weather
        rows += day_samples(day, cycles(2, minutes))
    run_list = runs(rows, WINDOW)
    per = daily(run_list, rows, tmean)["lennox:1"]
    assert per[0]["per_cdd"] == round(per[0]["cool_min"] / 20, 1)
    h = diagnose(per[:7], [r for r in run_list if r["start"] < START + timedelta(days=7)], [], date(2026, 7, 7))
    assert h["level"] == "good" and h["ratio"] == 1.0
    h = diagnose(per, run_list, [], date(2026, 7, 10))
    assert h["level"] == "serious" and h["ratio"] >= 1.4
    assert [a["action"] for a in h["actions"]] == ["filter", "coil", "service"]


def test_recent_filter_change_moves_to_coil():
    tmean = {(START + timedelta(days=d)).date().isoformat(): 85.0 for d in range(10)}
    rows = []
    for d in range(10):
        rows += day_samples(START + timedelta(days=d), cycles(2, 12 if d < 7 else 16))
    run_list = runs(rows, WINDOW)
    per = daily(run_list, rows, tmean)["lennox:1"]
    fix = [{"action": "filter", "at": "2026-07-08T09:00", "device_id": None}]
    h = diagnose(per, run_list, fix, date(2026, 7, 10))
    assert h["actions"][0]["action"] == "coil"


def test_short_cycling_and_cant_keep_up():
    rows = day_samples(START, cycles(6, 3))                                   # 3-minute runs, 6 an hour
    h = diagnose([], runs(rows, WINDOW), [], START.date())
    assert any("Short cycling" in e for e in h["evidence"])
    long = day_samples(START, lambda m: 600 <= m < 800, indoor=80, cool=75)    # 200 min, still 5 °F warm
    h = diagnose([], runs(long, WINDOW), [], START.date())
    assert h["level"] == "warning" and h["actions"][0]["action"] == "filter"


def test_fighting_thermostats():
    a = day_samples(START, lambda m: 600 <= m < 660, dev="nest:1", hvac="HEATING")
    b = day_samples(START, lambda m: 620 <= m < 700, dev="lennox:1")
    assert fighting(runs(a + b, WINDOW)) == 40


def test_build_prices_paid_hours_and_late_afternoon():
    rows = []
    tmean = {}
    for d in range(3):
        day = START + timedelta(days=d)
        tmean[day.date().isoformat()] = 88.0
        rows += day_samples(day, lambda m: 16 * 60 <= m < 20 * 60 and m % 30 < 20)   # 4–8 PM, 20 of every 30 min
    store = store_with(rows)
    out = build_hvac(store, [], TARIFF, tmean, {"lennox:1": 3.0}, START + timedelta(days=3), days=7)
    s = out["systems"][0]
    assert s["free_min"] == 0 and s["paid_min"] == 3 * 160
    assert abs(s["cost"]["paid"] - 3 * 160 / 60 * 3.0 * (22.8 + 7.1) * 1.04 / 100) < 0.02   # 8 kWh a day at 31.1¢
    assert s["days"][0]["paid_kwh"] == 8.0 and s["days"][0]["paid_usd"] == round(8 * 31.1 / 100, 2)
    recs = out["recommendations"]
    assert recs[0]["title"].startswith("Upstairs: $75 a month")
    assert any("from 4 PM to 8 PM costs $75" in r["title"] for r in recs)


def test_record_from_devices_and_log():
    class D:
        def __init__(self, **k): self.__dict__.update(k)
    store = HvacStore(sqlite3.connect(":memory:"))
    th = {"current": 75, "humidity": 48, "mode": "COOL", "hvac": "COOLING", "heat": 68, "cool": 74}
    n = store.record_devices([D(id="nest:a", name="Hall", kind="thermostat", online=True, thermostat=th),
                              D(id="kasa:x", name="Lamp", kind="plug", online=True, thermostat=None)], START)
    assert n == 1 and store.samples(START)[0]["hvac"] == "COOLING"
    m = store.log("filter", START)
    assert store.maintenance()[0]["id"] == m["id"]
