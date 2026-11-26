"""Home Assistant as the device layer, against a fake HA (REST + WebSocket), and the move-to-HA checklist."""

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from hub import migration
from hub.adapters.ha_adapter import HomeAssistant
from hub.config import Secrets
from hub.models import Device, HubError, StateIn

TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.test.token"
STATES = [
    {"entity_id": "cover.living_room_blinds", "state": "open", "attributes": {"friendly_name": "Living Room Blinds", "current_position": 60}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen", "brightness": 128, "supported_color_modes": ["brightness"]}},
    {"entity_id": "climate.hallway", "state": "heat", "attributes": {"friendly_name": "Hallway", "current_temperature": 69, "temperature": 68,
                                                                     "hvac_action": "heating", "hvac_modes": ["off", "heat", "cool", "heat_cool"]}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front door"}},
    {"entity_id": "sensor.kitchen_power", "state": "12", "attributes": {"friendly_name": "Kitchen power"}},
]
ENTITIES = [{"entity_id": "cover.living_room_blinds", "platform": "tuya", "device_id": "d1", "area_id": None},
            {"entity_id": "light.kitchen", "platform": "hue", "device_id": "d2", "area_id": "kitchen"},
            {"entity_id": "climate.hallway", "platform": "nest", "device_id": "d3", "area_id": None},
            {"entity_id": "lock.front_door", "platform": "schlage", "device_id": "d4", "area_id": None},
            {"entity_id": "sensor.kitchen_power", "platform": "tplink", "device_id": "d5", "area_id": None}]


class FakeHA:
    def __init__(self):
        self.calls, self.sockets = [], []
        app = web.Application()
        app.router.add_get("/api/config", self.config)
        app.router.add_get("/api/websocket", self.ws)
        self.server = TestServer(app)

    async def config(self, request):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"message": "Unauthorized"}, status=401)
        return web.json_response({"version": "2026.11.3", "location_name": "Home"})

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.11.3"})
        auth = await ws.receive_json()
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.11.3"})
        async for msg in ws:
            m = msg.json()
            result = None
            if m["type"] == "get_states":
                result = STATES
            elif m["type"] == "config/entity_registry/list":
                result = ENTITIES
            elif m["type"] == "config/device_registry/list":
                result = [{"id": "d1", "name": "Blinds", "area_id": "living", "model": "Curtain motor"}]
            elif m["type"] == "config/area_registry/list":
                result = [{"area_id": "living", "name": "Living Room"}, {"area_id": "kitchen", "name": "Kitchen"}]
            elif m["type"] == "call_service":
                self.calls.append((m["domain"], m["service"], m["target"]["entity_id"], m["service_data"]))
            await ws.send_json({"id": m["id"], "type": "result", "success": True, "result": result})
        return ws

    async def push(self, entity_id, new_state):
        for ws in self.sockets:
            await ws.send_json({"type": "event", "event": {"event_type": "state_changed",
                                                         "data": {"entity_id": entity_id, "new_state": new_state}}})


async def started(tmp_path):
    fake = FakeHA()
    await fake.server.start_server()
    ha = HomeAssistant(Secrets(tmp_path / "s.json"))
    url = str(fake.server.make_url("")).rstrip("/")
    return fake, ha, url


def test_connect_reads_states_registries_and_areas(tmp_path):
    async def go():
        fake, ha, url = await started(tmp_path)
        with pytest.raises(HubError) as err:
            await ha.test(url, "wrong-token-0000000000000")
        assert "rejected the token" in err.value.message
        out = await ha.connect(url, TOKEN)
        assert out["connected"] and out["version"] == "2026.11.3" and out["entities"] == 5
        assert ha.platform("cover.living_room_blinds") == "tuya" and ha.area("cover.living_room_blinds") == "Living Room"
        assert ha.area("light.kitchen") == "Kitchen" and ha.entities_for("tuya") == ["cover.living_room_blinds"]
        await ha.close()
        await fake.server.close()
    asyncio.run(go())


def test_moved_brands_appear_as_devices_and_take_commands(tmp_path):
    async def go():
        fake, ha, url = await started(tmp_path)
        await ha.connect(url, TOKEN)
        assert await ha.devices() == []                       # nothing moved yet
        ha.moved = {"tuya", "hue", "nest", "schlage"}
        devs = {d.id: d for d in await ha.devices()}
        blind, light = devs["ha:cover.living_room_blinds"], devs["ha:light.kitchen"]
        stat, lock = devs["ha:climate.hallway"], devs["ha:lock.front_door"]
        assert blind.kind == "blind" and blind.level == 60 and blind.brand == "tuya" and "position" in blind.capabilities
        assert light.on and light.brightness == 50 and "brightness" in light.capabilities
        assert stat.thermostat["mode"] == "HEAT" and stat.on and stat.thermostat["heat"] == 68
        assert lock.kind == "lock" and lock.on is True
        assert "ha:sensor.kitchen_power" not in devs
        await ha.set_state("ha:cover.living_room_blinds", StateIn(position=30))
        await ha.set_state("ha:cover.living_room_blinds", StateIn(action="stop"))
        await ha.set_state("ha:light.kitchen", StateIn(brightness=20))
        await ha.set_state("ha:light.kitchen", StateIn(on=False))
        await ha.set_state("ha:climate.hallway", StateIn(heat_f=70))
        await ha.set_state("ha:lock.front_door", StateIn(locked=True))
        assert fake.calls == [("cover", "set_cover_position", "cover.living_room_blinds", {"position": 30}),
                              ("cover", "stop_cover", "cover.living_room_blinds", {}),
                              ("light", "turn_on", "light.kitchen", {"brightness_pct": 20}),
                              ("light", "turn_off", "light.kitchen", {}),
                              ("climate", "set_temperature", "climate.hallway", {"temperature": 70}),
                              ("lock", "lock", "lock.front_door", {})]
        seen = []
        ha.on_change = lambda eid, old, new: seen.append((eid, new["state"]))
        await fake.push("cover.living_room_blinds", {"entity_id": "cover.living_room_blinds", "state": "closing",
                                                     "attributes": {"friendly_name": "Living Room Blinds", "current_position": 40}})
        for _ in range(50):
            await asyncio.sleep(0.01)
            if seen:
                break
        assert seen == [("cover.living_room_blinds", "closing")]
        assert {d.id: d for d in await ha.devices()}["ha:cover.living_room_blinds"].media == "Closing…"
        await ha.close()
        await fake.server.close()
    asyncio.run(go())


def test_the_checklist_matches_devices_and_guards_removal(tmp_path):
    async def go():
        fake, ha, url = await started(tmp_path)
        await ha.connect(url, TOKEN)
        secrets = ha.secrets
        secrets.set("tuya", {"access_id": "x", "access_secret": "y", "region": "us"})
        secrets.set("rainbird", {"host": "192.168.1.40", "password": "pw"})
        tuya = SimpleNamespace(brand="tuya", label="Smart Life", active=True, disconnected=False)

        async def disconnect():
            tuya.disconnected = True
            secrets.set("tuya", None)
        tuya.disconnect = disconnect
        adapters = [tuya, SimpleNamespace(brand="rainbird", label="Rain Bird", active=True),
                    SimpleNamespace(brand="smt", label="Smart Meter Texas", active=True)]
        hub = SimpleNamespace(ha=ha, secrets=secrets, adapters=adapters, device_cache=SimpleNamespace(get_if_present=lambda b: None),
                              last_devices={"tuya:b1": Device(id="tuya:b1", brand="tuya", name="Living Room Blinds", kind="blind"),
                                            "tuya:b2": Device(id="tuya:b2", brand="tuya", name="Office Shade", kind="blind")},
                              applied=0)
        hub.apply_migration = lambda: setattr(ha, "moved", {b for b, s in (secrets.get("ha_migration") or {}).items() if s.get("source") == "ha"})
        rows = {r["brand"]: r for r in migration.report(hub)["rows"]}
        t = rows["tuya"]
        assert (t["hub_devices"], t["ha_entities"], t["matched"], t["unmatched"]) == (2, 1, 1, ["Office Shade"])
        assert "User Code" in t["needs"] and t["source"] == "hub" and not t["moved"]
        assert rows["rainbird"]["hints"] == ["Controller IP: 192.168.1.40"] and "pw" not in str(rows["rainbird"])
        assert rows["smt"]["stays"] and "15-minute" in rows["smt"]["note"]
        migration.set_source(hub, "tuya", "ha")
        assert ha.moved == {"tuya"}
        await migration.remove_from_hub(hub, "tuya")
        assert tuya.disconnected and secrets.get("tuya") is None
        assert migration.report(hub)["rows"][[r["brand"] for r in migration.report(hub)["rows"]].index("tuya")]["moved"]
        await ha.close()
        await fake.server.close()
    asyncio.run(go())


def test_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        assert client.put("/api/ha/account", json={"url": "not a url", "token": TOKEN}, headers=token).status_code == 422
        report = client.get("/api/migration", headers=token).json()
        assert report["ha"]["configured"] is False and any(r["brand"] == "smt" and r["stays"] for r in report["rows"])
        assert client.put("/api/migration/tuya", json={"source": "ha"}, headers=token).status_code == 409   # HA not connected
        assert client.put("/api/migration/smt", json={"source": "ha"}, headers=token).status_code == 400    # stays in the hub
        assert client.post("/api/migration/tuya/remove", headers=token).status_code == 403                 # a person, not a script
        hub.ha.moved = {"tuya"}
        assert "tuya" not in {a.brand for a in hub.active_adapters()}
