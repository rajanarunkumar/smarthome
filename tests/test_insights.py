"""Water + sprinkler + weather analytics and smart program decisions, on synthetic data with known answers."""

import sqlite3
from datetime import date, datetime, timedelta

from hub.history import (HistoryStore, build_insights, daily_series, minutes_by_hour, overnight_leak,
                         recommendations, zone_flow)
from hub.irrigation import SmartPrograms, decide

TODAY = date(2026, 7, 20)


def store():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return HistoryStore(db)


def household(hour: int) -> float:
    return 6.0 if 6 <= hour <= 22 else 0.5  # gallons per hour without sprinklers


def fill(s: HistoryStore, gpm: dict[int, float], leak: float = 0.0, days: int = 14):
    """Zone 1 runs 20 min at 5:00, zone 2 runs 15 min at 5:20, every other day."""
    hourly = {}
    for n in range(days):
        d = TODAY - timedelta(days=n)
        for h in range(24):
            hourly[f"{d.isoformat()}T{h:02d}"] = household(h) + leak
        if n % 2 == 0:
            s.log_run(datetime(d.year, d.month, d.day, 5, 0), 1, 20)
            s.log_run(datetime(d.year, d.month, d.day, 5, 20), 2, 15)
            hourly[f"{d.isoformat()}T05"] += 20 * gpm[1] + 15 * gpm[2]
    s.put_water_hourly(hourly.items())
    daily = {}
    for k, v in hourly.items():
        daily[k[:10]] = daily.get(k[:10], 0) + v
    s.put_water_daily(daily.items())


def test_minutes_are_split_across_hours():
    runs = [{"started": "2026-07-20T05:50", "zone": 3, "minutes": 25.0}]
    assert minutes_by_hour(runs) == {"2026-07-20T05": {3: 10.0}, "2026-07-20T06": {3: 15.0}}


def test_zone_flow_recovers_each_zones_gpm():
    s = store()
    # Make zone 2 run in its own hour on some days so both zones can be measured alone.
    fill(s, {1: 8.0, 2: 5.0})
    for n in range(1, 14, 2):
        d = TODAY - timedelta(days=n)
        s.log_run(datetime(d.year, d.month, d.day, 3, 0), 2, 30)
        s.put_water_hourly([(f"{d.isoformat()}T03", household(3) + 30 * 5.0)])
    flow = zone_flow(s.runs(TODAY - timedelta(days=30)), s.water_hourly(TODAY - timedelta(days=30)))
    # Zone 1 never runs alone, but hours with zone 2 alone let the fit separate them.
    assert flow == {1: {"gpm": 8.0, "samples": 7}, 2: {"gpm": 5.0, "samples": 14}}


def test_zones_always_paired_identically_are_not_guessed():
    s = store()
    fill(s, {1: 8.0, 2: 5.0})  # zones 1 and 2 always share 5 AM in the same proportions
    assert zone_flow(s.runs(TODAY - timedelta(days=30)), s.water_hourly(TODAY - timedelta(days=30))) == {}


def test_leak_detection():
    s = store()
    fill(s, {1: 8.0, 2: 5.0}, leak=0.0)
    hourly = s.water_hourly(TODAY - timedelta(days=30))
    assert overnight_leak(hourly, set())["suspect"] is False
    s2 = store()
    fill(s2, {1: 8.0, 2: 5.0}, leak=3.0)
    leak = overnight_leak(s2.water_hourly(TODAY - timedelta(days=30)), set())
    assert leak["suspect"] and leak["gallons_per_hour"] == 3.5


def test_daily_series_splits_sprinkler_from_household():
    runs = [{"started": "2026-07-20T05:00", "zone": 1, "minutes": 20.0}]
    series = daily_series(["2026-07-20"], runs, {"2026-07-20": 400.0},
                          {"2026-07-20": {"rain_in": 0.1, "et0_in": 0.28, "tmax_f": 98, "forecast": 0}},
                          {1: {"gpm": 8.0, "samples": 5}})
    assert series[0] | {} == {"day": "2026-07-20", "total_gal": 400.0, "sprinkler_gal": 160.0, "other_gal": 240.0,
                              "minutes": 20.0, "rain_in": 0.1, "et0_in": 0.28, "tmax_f": 98, "forecast": False}


def weather_days(rain=0.0, et0=0.28, rain_recent=None):
    w = {}
    for n in range(-2, 10):
        d = (TODAY - timedelta(days=n)).isoformat()
        w[d] = {"rain_in": rain, "et0_in": et0, "tmax_f": 97, "forecast": int(n < 0)}
    for n, inches in (rain_recent or {}).items():
        w[(TODAY - timedelta(days=n)).isoformat()]["rain_in"] = inches
    return w


def test_decide_skips_after_rain_and_scales_with_evaporation():
    assert decide(weather_days(rain_recent={1: 0.6}), TODAY)["run"] is False
    assert decide(weather_days(rain_recent={0: 0.3}), TODAY)["run"] is False
    hot = decide(weather_days(et0=0.30), TODAY)
    assert hot["run"] and hot["factor"] == 1.2
    mild = decide(weather_days(et0=0.15), TODAY)
    assert mild["factor"] == 0.6 and "60%" in mild["reason"]
    assert decide({}, TODAY) == {"run": True, "factor": 1.0, "reason": "Ran at full time (no weather history yet)"}


def test_recommendations_compare_applied_to_needed():
    w = weather_days(et0=0.2)  # 7 days × 0.2 × 0.7 = 0.98 in needed
    series = daily_series(sorted(w), [], {}, w, {})
    flow = {1: {"gpm": 10.0, "samples": 4}, 2: {"gpm": 10.0, "samples": 4}}
    # Zone 1: 300 min × 10 gal / (1000 sq ft × 0.623) = 4.8 in: overwatered. Zone 2: 30 min -> 0.48 in: under.
    recs = recommendations(series, flow, {1: 300, 2: 30}, {1: 1000, 2: 1000}, lambda z: f"Zone {z}", None,
                           TODAY.isoformat())
    titles = [r["title"] for r in recs]
    assert "Lawn needs about 0.98 in this week" in titles
    assert "Zone 1: overwatered" in titles and "Zone 2: underwatered" in titles
    zone1 = next(r for r in recs if r["title"] == "Zone 1: overwatered")
    assert "Try 61 min a week instead of 300" in zone1["text"]


def test_build_insights_end_to_end():
    s = store()
    fill(s, {1: 8.0, 2: 5.0})
    s.put_weather([{"day": d, **v} for d, v in weather_days().items()])
    out = build_insights(s, lambda z: f"Zone {z}", {1: 1500}, days=14, today=TODAY)
    assert out["has_water"] and out["has_weather"] and out["has_runs"]
    assert len([r for r in out["series"] if r["forecast"]]) == 2
    assert out["summary"]["minutes"] == 7 * 35
    assert [z["zone"] for z in out["zones"]] == [1, 2]


class FakeRainBird:
    def __init__(self):
        self.sequences = []

    def run_sequence(self, zones, source="hub"):
        self.sequences.append(zones)


def test_smart_program_runs_scaled_and_logs_decisions():
    s = store()
    s.put_weather([{"day": d, **v} for d, v in weather_days(et0=0.15).items()])
    rb = FakeRainBird()
    sp = SmartPrograms(s.db, s, rb)
    sp.save({"name": "Lawn", "starts": ["05:00"], "days": ["mon"], "zones": [{"zone": 1, "minutes": 20},
             {"zone": 2, "minutes": 10}], "enabled": True, "smart": True})
    monday_5 = datetime(2026, 7, 20, 5, 0)  # 2026-07-20 is a Monday
    import asyncio
    assert asyncio.run(sp.run_due(monday_5)) == [1]
    assert asyncio.run(sp.run_due(monday_5)) == []  # once per minute
    assert rb.sequences == [[(1, 12), (2, 6)]]  # 60% of 20 and 10 minutes
    assert sp.decisions()[0]["reason"].startswith("Ran at 60%")
