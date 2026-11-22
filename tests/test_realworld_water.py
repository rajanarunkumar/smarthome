"""Real-world failure modes of the water-to-sprinkler chain, found in an independent review:
units, odd timestamps, a date format the server may refuse, running-total readings, clocks changing,
unrelated big water uses, sprinkler runs the hub missed, a controller that won't answer the combined
state request, and a flow test that reads a placeholder."""

import asyncio
import re
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from test_correlation import GPM, adapter_with, simulate  # noqa: E402

from hub.adapters.rainbird_adapter import RainBirdAdapter  # noqa: E402
from hub.adapters.water_adapter import hourly_rows, local_hour, looks_cumulative, parse_stamp, unit_factor  # noqa: E402
from hub.config import Secrets  # noqa: E402
from hub.flowtest import measure  # noqa: E402
from hub.history import align_hourly, build_insights, data_checks, best_hour_shift, minutes_by_hour, missing_runs  # noqa: E402

CHICAGO = ZoneInfo("America/Chicago")


def server(hourly_for_day, daily=None):
    """hourly_for_day(start, end) -> (status, json)."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/consumer/login":
            return httpx.Response(200, json={"token": "tok"})
        if path == "/consumer/meters":
            return httpx.Response(200, json=[{"meterCount": 1}])
        if path.startswith("/consumption/hourly/"):
            _, _, _, _, start, end = path.split("/")
            status, body = hourly_for_day(start, end)
            return httpx.Response(status, json=body)
        if path.startswith("/consumption/daily/"):
            return httpx.Response(200, json=daily or [])
        return httpx.Response(200, json=[])
    return handler


# 1. Units
def test_unit_table():
    assert unit_factor(None) == 1.0 and unit_factor("Gallons") == 1.0
    assert unit_factor("CCF") == 748.052 and unit_factor("cu. ft.") == 7.48052 and unit_factor("kgal") == 1000.0
    assert unit_factor("furlongs") is None


def test_meter_units_are_converted_to_gallons_hourly_and_daily(tmp_path):
    hourly = lambda start, end: (200, [{"consDate": f"{start}T05:00:00", "cons": 0.5, "units": "CCF"}])
    adapter = adapter_with(tmp_path, server(hourly, daily=[{"consDate": "2026-11-20", "cons": 2.0, "units": "CCF"}]))
    daily, hours = asyncio.run(adapter.history(days=1))
    assert dict(daily)["2026-11-20"] == pytest.approx(1496.104)
    assert all(v == pytest.approx(374.026) for _, v in hours)
    assert adapter.diagnostics["unit"] == "CCF" and adapter.diagnostics["unit_factor"] == 748.052
    checks = {c["id"]: c for c in data_checks([], {}, best_hour_shift({}, []), 0, {}, {}, 0, adapter.diagnostics)}
    assert checks["units"]["ok"] is True and "748.052" in checks["units"]["detail"]


def test_an_unknown_unit_is_reported_not_silently_assumed(tmp_path):
    hourly = lambda start, end: (200, [{"consDate": f"{start}T05:00:00", "cons": 9.0, "units": "furlongs"}])
    adapter = adapter_with(tmp_path, server(hourly))
    _, hours = asyncio.run(adapter.history(days=0))
    assert dict(hours) and all(v == 9.0 for v in dict(hours).values())  # unchanged, not guessed
    checks = {c["id"]: c for c in data_checks([], {}, best_hour_shift({}, []), 0, {}, {}, 0, adapter.diagnostics)}
    assert checks["units"]["ok"] is False and "furlongs" in checks["units"]["detail"]


# 2. Timestamps and amounts the parser has to survive
def test_stamp_formats():
    assert parse_stamp("11/20/2026 5:00 AM", CHICAGO) == (datetime(2026, 11, 20, 5), True)
    assert parse_stamp("11/20/2026 5:00 PM", CHICAGO) == (datetime(2026, 11, 20, 17), True)
    assert parse_stamp("11/20/2026 06:00", CHICAGO) == (datetime(2026, 11, 20, 6), True)
    assert parse_stamp("11-20-2026", CHICAGO) == (datetime(2026, 11, 20), False)
    utc_ms = int(datetime(2026, 11, 20, 13, tzinfo=timezone.utc).timestamp() * 1000)
    assert parse_stamp(utc_ms, CHICAGO) == (datetime(2026, 11, 20, 7), True)           # epoch milliseconds, local CST
    assert parse_stamp(str(utc_ms // 1000), CHICAGO) == (datetime(2026, 11, 20, 7), True)  # epoch seconds as text
    assert parse_stamp("garbage", CHICAGO) is None and parse_stamp(None, CHICAGO) is None and parse_stamp(12, CHICAGO) is None
    assert local_hour("2026-11-20", CHICAGO) is None                                     # a date alone isn't an hour


def test_unreadable_readings_are_counted_not_dropped_silently(tmp_path):
    utc_ms = int(datetime(2026, 11, 20, 13, tzinfo=timezone.utc).timestamp() * 1000)
    items = [{"consDate": "11/20/2026 5:00 AM", "cons": 3}, {"consDate": "11/20/2026 06:00", "cons": "4.5"},
             {"readDate": utc_ms, "consumption": 2}, {"consDate": "garbage", "cons": 1},
             {"consDate": "11/20/2026 08:00", "cons": None}, {"consDate": "11/20/2026 09:00", "cons": -3}]
    rows, bad = hourly_rows(items, CHICAGO)
    assert rows == [("2026-11-20T05", 3.0), ("2026-11-20T06", 4.5), ("2026-11-20T07", 2.0)]
    assert bad == ["garbage", "11/20/2026 08:00", "11/20/2026 09:00"]
    adapter = adapter_with(tmp_path, server(lambda s, e: (200, items)))
    asyncio.run(adapter.history(days=0))
    assert adapter.diagnostics["unparsed_items"] == 3 and adapter.diagnostics["bad_stamp"] == "garbage"
    checks = {c["id"]: c for c in data_checks([], {}, best_hour_shift({}, []), 0, {}, {}, 0, adapter.diagnostics)}
    assert checks["parse"]["ok"] is False and "garbage" in checks["parse"]["detail"]


def test_clocks_going_back_add_the_two_readings_in_the_repeated_hour():
    items = [{"consDate": "2026-11-01T06:00:00Z", "cons": 5}, {"consDate": "2026-11-01T07:00:00Z", "cons": 7}]
    assert hourly_rows(items, CHICAGO)[0] == [("2026-11-01T01", 12.0)]   # 06:00Z is 1 AM CDT and 07:00Z is 1 AM CST


# 3. A date format the server may refuse
def test_hourly_url_falls_back_to_the_us_date_format_the_daily_endpoint_uses(tmp_path):
    def hourly(start, end):
        if re.match(r"\d{4}-", start):
            return 400, {"error": "invalid date"}
        month, day, year = start.split("-")
        return 200, [{"consDate": f"{year}-{month}-{day}T05:00:00", "cons": 10.0}]
    adapter = adapter_with(tmp_path, server(hourly))
    _, hours = asyncio.run(adapter.history(days=1))
    assert len(hours) == 2 and adapter._hourly_fmt == "mdy"
    diag = adapter.diagnostics
    assert (diag["hourly_failures"], diag["hourly_days_with_data"], diag["hourly_url_format"]) == (0, 2, "MM-DD-YYYY")


# 4. A running total instead of use per hour
def test_a_running_meter_total_is_converted_to_use_per_hour(tmp_path):
    totals = [10000 + 50 * i + (i % 3) * 10 for i in range(24)]
    assert looks_cumulative(totals)
    assert not looks_cumulative([0, 0, 0, 0.5, 3, 12, 8, 5, 4, 3, 2, 6, 9, 7, 4, 3, 2, 1, 1, 5, 11, 9, 4, 1])  # ordinary use
    assert not looks_cumulative([5.0] * 24) and not looks_cumulative(totals[:6])
    items = lambda start, end: (200, [{"consDate": f"{start}T{h:02d}:00:00", "cons": v} for h, v in enumerate(totals)])
    adapter = adapter_with(tmp_path, server(items))
    _, hours = asyncio.run(adapter.history(days=0))
    assert len(hours) == 23 and all(25 <= v <= 65 for _, v in hours) and adapter.diagnostics["cumulative"] is True


# 5. The meter stamps in UTC with no label, across a daylight-saving change
def test_unlabelled_utc_is_converted_with_daylight_saving(tmp_path):
    today = date(2026, 11, 8)  # the window spans the clocks going back on Nov 1
    store = simulate(tmp_path, 0, today=today, utc_tz="America/Chicago")
    out = build_insights(store, lambda z: f"Zone {z}", {}, days=21, today=today, tz_name="America/Chicago")
    assert out["alignment"]["mode"] == "utc" and out["alignment"]["confident"]
    zones = {z["zone"]: z["gpm"] for z in out["zones"]}
    assert zones == GPM, out["alignment"]
    assert "UTC" in next(c for c in out["checks"] if c["id"] == "clock")["detail"]


# 6. Unrelated big water uses during watering hours
def test_two_pool_fills_in_watering_hours_do_not_spoil_the_fit(tmp_path):
    from test_correlation import TODAY
    store = simulate(tmp_path, 0)
    since = TODAY - timedelta(days=30)
    watering = sorted(minutes_by_hour(store.runs(since)))
    hourly = store.water_hourly(since)
    store.put_water_hourly([(watering[2], hourly[watering[2]] + 300.0), (watering[7], hourly[watering[7]] + 300.0)])
    out = build_insights(store, lambda z: f"Zone {z}", {}, days=21, today=TODAY)
    zones = {z["zone"]: z["gpm"] for z in out["zones"]}
    assert all(abs(zones[z] - GPM[z]) <= 0.3 for z in GPM), zones


# 7. Sprinkler runs the hub missed
def test_replay_fills_days_the_hub_missed_and_leaves_recorded_days_alone():
    recorded = [{"started": "2026-11-17T05:00", "zone": 1, "minutes": 20, "source": "controller"},
                {"started": "2026-11-15T05:00", "zone": 1, "minutes": 20, "source": "inferred"}]  # an earlier guess, not a record
    replayed = [{"started": f"2026-11-{d}T05:00", "zone": 1, "minutes": 20} for d in (15, 17, 19)]
    assert [r["started"][:10] for r in missing_runs(replayed, recorded)] == ["2026-11-15", "2026-11-19"]


# 8. A controller that won't answer the combined state request
def test_older_controllers_are_read_with_zone_and_rain_delay_requests(tmp_path):
    class Old:
        async def get_combined_controller_state(self):
            raise RuntimeError("NACK: command not supported")

        async def get_zone_states(self):
            return SimpleNamespace(active_set={2})

        async def get_rain_delay(self):
            return 1

        async def get_rain_sensor_state(self):
            return True

    adapter = RainBirdAdapter(Secrets(tmp_path / "s.json"))
    state = asyncio.run(adapter._read_state(Old()))
    assert (state.irrigation_state, state.active_station, state.delay_setting, state.sensor_state) == (1, 2, 1, 1)
    assert adapter._state_mode == "legacy"
    logged = []
    adapter.on_run = lambda started, zone, minutes, source: logged.append(zone)
    adapter.observe(state, datetime(2026, 11, 20, 5, 0))
    adapter.observe(SimpleNamespace(irrigation_state=0, active_station=0), datetime(2026, 11, 20, 5, 10))
    assert logged == [2]   # a run is recorded even without the combined state

    class Good:
        async def get_combined_controller_state(self):
            return SimpleNamespace(irrigation_state=0, active_station=0)

    adapter2 = RainBirdAdapter(Secrets(tmp_path / "s2.json"))
    asyncio.run(adapter2._read_state(Good()))
    assert adapter2._state_mode == "combined"

    class Breaks(Good):
        async def get_combined_controller_state(self):
            raise ConnectionError("lost")
    with pytest.raises(ConnectionError):  # once it has worked, a failure is real and is reported, not papered over
        asyncio.run(adapter2._read_state(Breaks()))


def test_the_data_check_names_the_controller_error_when_no_runs_were_seen():
    checks = {c["id"]: c for c in data_checks([], {}, best_hour_shift({}, []), 0, {}, {}, 0, None,
                                              watch_error="RainbirdApiException: NACK")}
    assert "couldn't read the Rain Bird controller: RainbirdApiException: NACK" in checks["runs"]["detail"]


# 9. A flow test that reads a placeholder
def test_a_zero_reading_is_not_taken_as_the_flow_test_result():
    hourly = {f"2026-11-{d:02d}T02": 1.0 for d in range(10, 22)}
    hourly["2026-11-22T02"] = 1.0           # the meter posted the hour as normal use (a placeholder, or its clock is off)
    assert measure({"start": "2026-11-22T02:05", "minutes": 10}, hourly, set()) is None
    hourly["2026-11-22T02"] = 1.0 + 10 * 7.5
    assert measure({"start": "2026-11-22T02:05", "minutes": 10}, hourly, set())["gpm"] == 7.5
