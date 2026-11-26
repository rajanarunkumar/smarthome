"""The whole water-to-sprinkler chain: controller runs -> log -> meter hours -> per-zone gal/min.

Earlier tests covered each piece alone; these run the pieces together, with the meter's clock correct,
an hour off (end-of-hour stamps) and six hours off (UTC without a label).
"""

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from hub.adapters import water_adapter
from hub.adapters.rainbird_adapter import RainBirdAdapter
from hub.adapters.water_adapter import WaterAdvisorAdapter, hourly_usage, local_hour
from hub.config import Secrets
from hub.history import HistoryStore, apply_shift, best_hour_shift, build_insights, minutes_by_hour
from hub.models import HubError

TODAY = date(2026, 7, 20)
GPM = {1: 8.0, 2: 5.0, 3: 3.0}


def state(zone=0):
    return SimpleNamespace(irrigation_state=1 if zone else 0, active_station=zone, remaining_runtime=0,
                           delay_setting=0, sensor_state=0)


def household(hour: int) -> float:
    return 6.0 if 6 <= hour <= 22 else 0.5


def simulate(tmp_path, offset_hours: int, every_day: bool = False, today: date = TODAY, utc_tz: str | None = None):
    """21 days of 5 AM watering (every other day, or every day) seen through the controller poller, and a
    meter whose hour labels are `offset_hours` late (or, with utc_tz, are UTC with no label). Each zone's minutes
    vary so zones can be told apart."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = HistoryStore(db)
    adapter = RainBirdAdapter(Secrets(tmp_path / "s.json"))
    adapter.on_run = store.log_run  # exactly how the hub wires it
    runs = []
    for n in range(21):
        if n % 2 and not every_day:
            continue
        k = n if every_day else n // 2
        t = datetime.combine(today - timedelta(days=n), datetime.min.time()).replace(hour=5)
        for zone, minutes in ((1, 20 + (k % 3) * 5), (2, 12 + (k % 2) * 6), (3, 10 + (k % 4) * 3)):
            adapter.observe(state(zone), t)           # the poller sees this zone start...
            runs.append({"started": t.isoformat(timespec="minutes"), "zone": zone, "minutes": minutes})
            t += timedelta(minutes=minutes)
        adapter.observe(state(0), t)                   # ...and the program finish
    by_hour = minutes_by_hour(runs)
    hourly = {}
    for n in range(21):
        for h in range(24):
            key = f"{(today - timedelta(days=n)).isoformat()}T{h:02d}"
            hourly[key] = household(h) + sum(m * GPM[z] for z, m in by_hour.get(key, {}).items())
    stamped = {}
    for key, gallons in hourly.items():
        t = datetime.strptime(key, "%Y-%m-%dT%H") + timedelta(hours=offset_hours)
        if utc_tz:  # the real local hour, written down in UTC with no label
            t = t.replace(tzinfo=ZoneInfo(utc_tz)).astimezone(timezone.utc).replace(tzinfo=None)
        stamped[t.strftime("%Y-%m-%dT%H")] = gallons
    store.put_water_hourly(stamped.items())
    store.put_water_daily({d: sum(v for k, v in hourly.items() if k[:10] == d) for d in {k[:10] for k in hourly}}.items())
    return store


@pytest.mark.parametrize("offset", [0, 1, 6])
def test_zone_gpm_is_found_whatever_the_meter_clock_says(tmp_path, offset):
    store = simulate(tmp_path, offset)
    out = build_insights(store, lambda z: f"Zone {z}", {}, days=21, today=TODAY)
    zones = {z["zone"]: z for z in out["zones"]}
    assert {z: zones[z]["gpm"] for z in GPM} == GPM, out["checks"]
    assert out["alignment"]["confident"] and out["alignment"]["shift"] == -offset
    assert all(c["ok"] is True for c in out["checks"] if c["id"] != "weather"), out["checks"]
    assert out["summary"]["sprinkler_gal"] > 0 and out["summary"]["sprinkler_share"] > 0.1


def test_an_hour_watered_every_day_is_not_guessed_and_the_app_says_why(tmp_path):
    """No watering-free day for 5 AM means no baseline: no per-zone numbers rather than biased ones."""
    store = simulate(tmp_path, 0, every_day=True)
    out = build_insights(store, lambda z: f"Zone {z}", {}, days=21, today=TODAY)
    zones = {z["zone"]: z for z in out["zones"]}
    assert zones[1]["gpm"] is None and zones[2]["gpm"] is None   # they only ever run in the hour with no baseline
    assert zones[3]["gpm"] in (None, 3.0)                         # zone 3 spills into 6 AM, which has clean days: if known, correct
    zones_check = next(c for c in out["checks"] if c["id"] == "zones")
    assert zones_check["ok"] is False and "watered every day" in zones_check["detail"] and "flow tests" in zones_check["detail"]


def test_checks_say_exactly_what_is_missing(tmp_path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = HistoryStore(db)
    empty = {c["id"]: c for c in build_insights(store, str, {}, days=14, today=TODAY)["checks"]}
    assert empty["runs"]["ok"] is False and "30 seconds" in empty["runs"]["detail"]
    assert empty["hourly"]["ok"] is False and empty["overlap"]["ok"] is None and empty["clock"]["ok"] is None

    # Runs but no hourly water, and the site refused every hourly request:
    store.log_run(datetime(2026, 7, 20, 5, 0), 1, 20)
    diag = {"hourly_days_requested": 29, "hourly_days_with_data": 0, "hourly_failures": 29, "last_error": "My Water Advisor said 400"}
    checks = {c["id"]: c for c in build_insights(store, str, {}, days=14, today=TODAY, water_diag=diag)["checks"]}
    assert checks["runs"]["ok"] is True
    assert "refused or failed 29 of 29" in checks["hourly"]["detail"] and "400" in checks["hourly"]["detail"]
    assert "flow tests" in checks["hourly"]["detail"]

    # The site answered but sent nothing:
    quiet = dict(diag, hourly_failures=0)
    detail = {c["id"]: c for c in build_insights(store, str, {}, days=14, today=TODAY, water_diag=quiet)["checks"]}["hourly"]["detail"]
    assert "no hourly readings" in detail and "daily totals" in detail


def test_alignment_needs_real_evidence():
    assert best_hour_shift({}, [])["confident"] is False
    runs = [{"started": f"2026-07-{d:02d}T05:00", "zone": 1, "minutes": 20} for d in range(1, 4)]
    flat = {f"2026-07-{d:02d}T{h:02d}": 5.0 for d in range(1, 8) for h in range(24)}   # no response to watering
    result = best_hour_shift(flat, runs * 3)
    assert result["confident"] is False
    assert apply_shift({"2026-07-20T23": 1.0}, 2) == {"2026-07-21T01": 1.0}


def test_timestamps_are_read_as_local_time():
    chicago = ZoneInfo("America/Chicago")
    assert local_hour("2026-11-20T05:00:00", chicago) == "2026-11-20T05"            # no label: already local
    assert local_hour("2026-11-20T11:00:00.000Z", chicago) == "2026-11-20T05"       # UTC in winter (CST, UTC-6)
    assert local_hour("2026-07-20T10:00:00Z", chicago) == "2026-07-20T05"           # UTC in summer (CDT, UTC-5)
    assert local_hour("2026-11-20T05:00:00-06:00", chicago) == "2026-11-20T05"
    assert local_hour("2026-11-20 05:00:00", chicago) == "2026-11-20T05"
    assert local_hour("2026-11", chicago) is None
    assert hourly_usage([{"consDate": "2026-11-20T11:00:00Z", "cons": "12.5"}, {"consDate": "bad", "cons": 1}], chicago) == [("2026-11-20T05", 12.5)]


def meter_server(requests, fail_on=None, ignore_dates=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/consumer/login":
            return httpx.Response(200, json={"token": "tok"})
        if request.url.path == "/consumer/meters":
            return httpx.Response(200, json=[{"meterCount": 1}])
        if request.url.path.startswith("/consumption/hourly/"):
            _, _, _, count, start, end = request.url.path.split("/")
            requests.append((start, end))
            if start == fail_on:
                return httpx.Response(500, json={"error": "boom"})
            day = "2026-07-01" if ignore_dates else start
            return httpx.Response(200, json=[{"consDate": f"{day}T05:00:00", "cons": 10.0},
                                             {"consDate": f"{day}T06:00:00", "cons": 4.0}])
        return httpx.Response(200, json=[])
    return handler


def adapter_with(tmp_path, handler):
    adapter = WaterAdvisorAdapter(Secrets(tmp_path / "s.json"))
    adapter._http = httpx.AsyncClient(base_url=water_adapter.BASE_URL, transport=httpx.MockTransport(handler))
    asyncio.run(adapter.connect("me@example.com", "pw"))
    return adapter


def test_hourly_water_is_requested_one_day_at_a_time_like_the_website(tmp_path):
    requests = []
    adapter = adapter_with(tmp_path, meter_server(requests, fail_on=(date.today() - timedelta(days=2)).isoformat()))
    daily, hourly = asyncio.run(adapter.history(days=3))
    assert len(requests) == 4 and all(start == end for start, end in requests)          # today and 3 days back, start == end
    assert len({start for start, _ in requests}) == 4
    diag = adapter.diagnostics
    assert (diag["hourly_days_requested"], diag["hourly_days_with_data"], diag["hourly_failures"]) == (4, 3, 1)
    assert "500" in diag["last_error"] or "My Water Advisor said" in diag["last_error"]
    assert len(hourly) == 6 and all(k[11:13] in ("05", "06") for k, _ in hourly)
    assert diag["sample_stamp"].endswith("T05:00:00")


def test_a_service_that_ignores_the_dates_does_not_double_count(tmp_path):
    adapter = adapter_with(tmp_path, meter_server([], ignore_dates=True))
    _, hourly = asyncio.run(adapter.history(days=3))
    assert dict(hourly) == {"2026-07-01T05": 10.0, "2026-07-01T06": 4.0}   # same hours repeated 4 times, counted once


def test_every_hourly_request_failing_is_reported_not_hidden(tmp_path):
    def handler(request):
        if request.url.path.startswith("/consumption/hourly/"):
            return httpx.Response(400, json={"error": "range too long"})
        return meter_server([])(request)
    adapter = adapter_with(tmp_path, handler)
    _, hourly = asyncio.run(adapter.history(days=2))
    assert hourly == [] and adapter.diagnostics["hourly_failures"] == 3 and adapter.diagnostics["hourly_days_with_data"] == 0
