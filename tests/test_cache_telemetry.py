"""L1 loading cache (Caffeine-style), request telemetry and the Admin endpoints."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from hub.cache import LoadingCache, percentile


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


def make(loader, clock, **kw):
    return LoadingCache("t", loader, clock=clock, refresh_after=10, expire_after=60, **kw)


def test_percentile():
    assert percentile([], 95) is None
    assert percentile([5], 50) == 5
    assert percentile(range(1, 101), 95) == 95
    assert percentile(range(1, 101), 50) == 50


def test_single_flight_and_hits():
    calls = []

    async def loader(key):
        calls.append(key)
        await asyncio.sleep(0.01)
        return f"v-{key}"

    async def main():
        c = make(loader, Clock())
        values = await asyncio.gather(*(c.get("a") for _ in range(10)))
        assert values == ["v-a"] * 10
        assert calls == ["a"]
        assert await c.get("a") == "v-a"
        s = c.snapshot()
        assert s["loads"] == 1 and s["hits"] == 1 and s["misses"] == 10
    run(main())


def test_refresh_after_write_serves_old_value_and_reloads_once():
    clock, n = Clock(), {"v": 0}

    async def loader(key):
        n["v"] += 1
        return n["v"]

    async def main():
        c = make(loader, clock)
        assert await c.get() == 1
        clock.t += 15  # past refresh_after, before expiry
        assert await c.get() == 1 and await c.get() == 1  # old value, one background reload
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert await c.get() == 2
        assert n["v"] == 2 and c.stats.refreshes == 1
    run(main())


def test_expired_entry_waits_and_failed_reload_serves_stale():
    clock, mode = Clock(), {"fail": False}

    async def loader(key):
        if mode["fail"]:
            raise RuntimeError("controller locked")
        return "good"

    async def main():
        c = make(loader, clock)
        assert await c.get() == "good"
        clock.t += 120
        mode["fail"] = True
        assert await c.get() == "good"  # stale but served
        e = c.entry()
        assert e.stale and "locked" in e.error
        assert c.stats.load_failures == 1 and c.stats.stale_serves >= 1
        # Backoff: no reload hammering right after a failure
        clock.t += 1
        e.written = clock.t - 15  # due for refresh, but inside failure backoff
        before = c.stats.loads
        await c.get()
        assert c.stats.loads == before
    run(main())


def test_timeout_without_value_raises_and_load_finishes_in_background():
    async def loader(key):
        await asyncio.sleep(0.05)
        return "late"

    async def main():
        c = make(loader, Clock(), timeout=0.01)
        with pytest.raises(asyncio.TimeoutError):
            await c.get()
        await asyncio.sleep(0.08)
        assert c.get_if_present() == "late"
        assert c.stats.timeouts == 1
    run(main())


def test_lru_eviction_and_invalidate():
    async def loader(key):
        return key

    async def main():
        c = make(loader, Clock(), max_size=2)
        await c.get("a"); await c.get("b"); await c.get("a"); await c.get("c")
        assert set(c.keys()) == {"a", "c"} and c.stats.evictions == 1
        c.invalidate("a")
        assert c.keys() == ["c"]
        c.invalidate_all()
        assert len(c) == 0
    run(main())


def test_refresh_bypasses_freshness():
    n = {"v": 0}

    async def loader(key):
        n["v"] += 1
        return n["v"]

    async def main():
        c = make(loader, Clock())
        await c.get()
        assert await c.refresh() == 2
    run(main())


# ----- hub integration -----
ORIGIN = "https://hub.example.ts.net"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from hub import app as appmod

    hub = appmod.Hub()
    hub.secrets.set("auth", {"google_client_id": "cid", "allowed_emails": ["me@example.com"],
                             "origins": [ORIGIN], "session_hours": 12})
    hub._by_prefix = {p: a for a in hub.adapters for p in a.prefixes}
    appmod.app.state.hub = hub
    client = TestClient(appmod.app, base_url=ORIGIN)
    token = hub.secrets.access_token()
    return hub, client, {"Authorization": f"Bearer {token}"}


class SlowAdapter:
    brand, label, prefixes, active, can_rename = "slow", "Slow", ("slow",), True, False

    def __init__(self, delay, devices):
        self.delay, self._devices, self.calls = delay, devices, 0

    async def devices(self):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self._devices

    async def status(self):
        return {"devices": len(self._devices)}

    async def scenes(self):
        return []

    async def schedules(self):
        return []


def test_slow_brand_does_not_block_and_is_reported(env, monkeypatch):
    from hub import app as appmod
    from hub.models import Device

    hub, client, auth = env
    monkeypatch.setattr(appmod, "DEVICE_TIMEOUT", 0.05)
    hub.device_cache.timeout = 0.05
    slow = SlowAdapter(0.3, [Device(id="slow:1", brand="slow", name="Slow lamp", kind="light")])
    hub.adapters.append(slow)
    hub._by_brand["slow"] = slow
    r = client.get("/api/devices", headers=auth)
    assert r.status_code == 200
    assert any("slow" in e and "still trying" in e for e in r.json()["errors"])


def test_telemetry_counts_requests_and_admin_needs_person_for_actions(env):
    hub, client, auth = env
    client.get("/api/health")
    client.get("/api/health")
    client.get("/api/nope", headers=auth)
    r = client.get("/api/admin/telemetry", headers=auth)
    assert r.status_code == 200
    data = r.json()
    routes = {x["route"]: x for x in data["requests"]["routes"]}
    assert routes["GET /api/health"]["count"] == 2
    assert {c["name"] for c in data["caches"]} == {"devices", "scenes", "schedules"}
    assert data["process"]["pid"] > 0 and "load_avg" in data["host"]
    # The script token can read Admin but not press its buttons.
    assert client.post("/api/admin/refresh/raw", headers=auth, json={}).status_code == 403
    assert client.post("/api/admin/cache/refresh", headers=auth, json={}).status_code == 403


def test_raw_refresh_is_rate_limited_per_brand(env):
    from hub.models import Device

    hub, client, auth = env
    fast = SlowAdapter(0, [Device(id="slow:1", brand="slow", name="Lamp", kind="light")])
    hub.adapters.append(fast)
    hub._by_brand["slow"] = fast

    async def main():
        first = await hub.raw_refresh(["slow"])
        second = await hub.raw_refresh(["slow"])
        return first, second
    first, second = run(main())
    assert first["brands"]["slow"]["ok"] and first["brands"]["slow"]["devices"] == 1
    assert second["brands"]["slow"].get("skipped")
    assert fast.calls == 1


def test_running_load_past_timeout_answers_at_once_and_failed_first_load_backs_off():
    clock = Clock()
    calls = {"n": 0}

    async def slow(key):
        calls["n"] += 1
        await asyncio.sleep(0.2)
        raise RuntimeError("down")

    async def main():
        c = make(slow, clock, timeout=0.02)
        with pytest.raises(asyncio.TimeoutError):
            await c.get()
        clock.t += 1  # the load has now been running longer than the timeout
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.TimeoutError):
            await c.get()
        assert asyncio.get_running_loop().time() - t0 < 0.01
        await asyncio.sleep(0.25)  # load fails
        with pytest.raises(RuntimeError):
            await c.get()  # remembered failure, no new load inside the backoff
        assert calls["n"] == 1
        clock.t += 31
        with pytest.raises(asyncio.TimeoutError):
            await c.get()  # backoff over: tries again
        assert calls["n"] == 2
    run(main())
