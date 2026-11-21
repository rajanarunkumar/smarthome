"""Hub-run schedules, Smart Meter Texas and My Water Advisor."""

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta

import httpx
import pytest

from hub.adapters import water_adapter
from hub.adapters.smt_adapter import day_total
from hub.adapters.water_adapter import WaterAdvisorAdapter, daily_usage
from hub.config import Secrets
from hub.models import Action, HubError, ScheduleIn, Trigger
from hub.scheduler import HubScheduler


def scheduler():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    calls = []

    async def apply(device_id, state):
        calls.append((device_id, state.on))
    return HubScheduler(db, apply), calls


def test_hub_schedule_runs_once_at_its_minute():
    sched, calls = scheduler()
    spec = ScheduleIn(name="Pump at 8", device_ids=["pentair:p1", "govee:x"], action=Action(on=True),
                      trigger=Trigger(type="time", time="08:00"), days=["mon", "wed"])
    [made] = asyncio.run(sched.create_schedule(spec, spec.device_ids))
    assert (made.runs_on, made.trigger.time, made.origin) == ("Hub (this Mac)", "08:00", "hub")
    monday_8 = datetime(2026, 11, 23, 8, 0, 5)
    assert asyncio.run(sched.run_due(monday_8)) == [int(made.id.split(":")[1])]
    assert asyncio.run(sched.run_due(monday_8.replace(second=40))) == []  # not twice in one minute
    assert asyncio.run(sched.run_due(datetime(2026, 11, 24, 8, 0))) == []  # Tuesday isn't a chosen day
    assert calls == [("pentair:p1", True), ("govee:x", True)]
    asyncio.run(sched.set_schedule_enabled(made.id, False))
    assert asyncio.run(sched.run_due(datetime(2026, 11, 25, 8, 0))) == []  # disabled
    asyncio.run(sched.delete_schedule(made.id))
    assert asyncio.run(sched.schedules()) == []


def test_hub_schedules_refuse_locks_and_sunset():
    sched, _ = scheduler()
    lock = ScheduleIn(device_ids=["schlage:L1"], action=Action(on=True), trigger=Trigger(time="22:00"), days=["mon"])
    with pytest.raises(HubError):
        asyncio.run(sched.create_schedule(lock, lock.device_ids))
    sunset = ScheduleIn(device_ids=["govee:x"], action=Action(on=True), trigger=Trigger(type="sunset"), days=["mon"])
    with pytest.raises(HubError):
        asyncio.run(sched.create_schedule(sunset, sunset.device_ids))


def test_smart_meter_texas_day_total():
    intervals = [["11/20/2026 0:00", "0.25"], ["11/20/2026 0:15", "0.5"], ["11/20/2026 0:30", ""]]
    assert day_total(intervals) == 0.8 and day_total([]) is None


def test_water_daily_usage_parsing():
    items = [{"consDate": "2026-11-20T00:00:00", "cons": 180.4, "units": "Gallons"}, {"consDate": "2026-11-19", "cons": "95"},
             {"consDate": "2026-11-18", "cons": None}]
    assert daily_usage(items) == [("2026-11-19", 95.0), ("2026-11-20", 180.4)]


def test_water_adapter_signs_in_and_reads(tmp_path, monkeypatch):
    seen = []
    today = date.today()
    days = [{"consDate": (today - timedelta(days=i)).isoformat(), "cons": 100 + i, "units": "Gallons"} for i in range(3)]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/consumer/login":
            body = json.loads(request.content)
            assert request.headers["x-app-id"] == water_adapter.APP_ID and body["type"] == 1 and body["osType"] == 3
            return httpx.Response(200, json={"token": "tok"} if body["pw"] == "right" else {"error": "Wrong password"})
        assert request.headers["x-access-token"] == "tok"
        if request.url.path == "/consumer/meters":
            return httpx.Response(200, json=[{"meterCount": 1, "meterSn": "A123", "fullAddress": "1 Main St"}])
        return httpx.Response(200, json=days)

    adapter = WaterAdvisorAdapter(Secrets(tmp_path / "s.json"))
    adapter._http = httpx.AsyncClient(base_url=water_adapter.BASE_URL, transport=httpx.MockTransport(handler))
    with pytest.raises(HubError):
        asyncio.run(adapter.connect("me@example.com", "wrong"))
    assert asyncio.run(adapter.connect("me@example.com", "right")) == {"connected": True, "devices": 1}
    [d] = asyncio.run(adapter.devices())
    assert (d.kind, d.name, d.media) == ("meter", "Water", "Latest day 101 gallons")
    assert d.details[1] == {"label": "Last 7 days", "value": "303 gallons"}
    daily = [p for m, p in seen if p.startswith("/consumption/daily/")][0]
    assert daily.startswith("/consumption/daily/1/")
