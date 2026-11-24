"""Work follows use: screens kept current only while the app is used, insights reused until new data."""

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from hub import app as app_module
from hub.models import Device


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        yield hub, client, {"Authorization": f"Bearer {hub.secrets.access_token()}"}


def test_requests_mark_the_app_in_use(env):
    hub, client, token = env
    hub.last_use = float("-inf")
    assert not hub.in_use()
    client.get("/api/health")                     # public, nobody signed in: not "use"
    assert not hub.in_use()
    client.get("/api/rooms", headers=token)
    assert hub.in_use()
    hub.last_use = time.monotonic() - app_module.IN_USE_SECONDS - 1
    assert not hub.in_use()


def test_insights_are_reused_until_new_data_or_a_change(env, monkeypatch):
    hub, client, token = env
    calls = []
    real = app_module.build_insights

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)
    monkeypatch.setattr(app_module, "build_insights", counting)
    first = client.get("/api/insights?days=14", headers=token)
    second = client.get("/api/insights?days=14", headers=token)
    assert first.json() == second.json() and len(calls) == 1
    assert first.headers["X-Insight-Cache"] == "miss" and second.headers["X-Insight-Cache"] == "hit"
    client.get("/api/insights?days=21", headers=token)                       # other parameters: computed
    assert len(calls) == 2
    client.get("/api/insights?days=14&fresh=1", headers=token)               # the refresh button
    assert len(calls) == 3
    hub.data_version += 1                                                    # the 4-hourly collection
    client.get("/api/insights?days=14", headers=token)
    assert len(calls) == 4
    client.post("/api/rooms", json={"name": "Den"}, headers=token)           # any change in the app
    client.get("/api/insights?days=14", headers=token)
    assert len(calls) == 5


def test_the_warmer_does_nothing_while_nobody_uses_the_app(env, monkeypatch):
    hub, _, _ = env
    warmed = []
    monkeypatch.setattr(hub.device_cache, "warm", lambda keys: warmed.append(list(keys)) or len(keys))
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError
        await real_sleep(0)
    monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)

    async def run():
        try:
            await hub._warm_loop()
        except asyncio.CancelledError:
            pass
    hub.last_use = float("-inf")
    asyncio.run(run())
    assert warmed == []
    sleeps.clear()
    hub.note_use()
    monkeypatch.setattr(hub, "active_adapters", lambda: [hub.kasa, hub.thinq])
    hub.device_cache.put("thinq", [])                                       # cloud brand loaded just now
    asyncio.run(run())
    assert warmed and all(w == ["kasa"] for w in warmed)                    # local every 15 s, cloud only after 60 s


def test_a_brand_not_read_this_time_is_not_marked_off(tmp_path):
    import sqlite3

    from hub.energy import EnergyStore
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    s = EnergyStore(db)
    t0 = datetime(2026, 11, 20, 12, 0)
    dryer = Device(id="thinq:1", brand="thinq", name="Dryer", kind="appliance", on=True)
    stat = Device(id="lennox:1", brand="lennox", name="Thermostat", kind="thermostat", on=True)
    s.observe([dryer, stat], t0, scope=("thinq:", "lennox:"))
    s.observe([stat], t0 + timedelta(minutes=10), scope=("lennox:",))      # idle tick: only thermostats read
    assert s.activity(t0.date()) == []                                     # the dryer is still running as far as we know
    s.observe([stat], t0 + timedelta(minutes=20), scope=("thinq:", "lennox:"))   # read again: now it's off
    assert [a["device_id"] for a in s.activity(t0.date())] == ["thinq:1"]


def test_lennox_and_rainbird_slow_down_when_idle():
    from hub.adapters import lennox_adapter, rainbird_adapter
    from hub.config import Secrets
    rb = rainbird_adapter.RainBirdAdapter(Secrets())
    assert rb._watch_seconds() == rainbird_adapter.WATCH_SECONDS
    rb.idle = lambda: True
    assert rb._watch_seconds() == rainbird_adapter.IDLE_WATCH_SECONDS
    assert lennox_adapter.IDLE_POLL_SECONDS > lennox_adapter.CLOUD_POLL_SECONDS
    assert app_module.COLLECT_SECONDS == 4 * 3600
