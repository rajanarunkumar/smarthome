"""Smart Life / Tuya: blinds (open, close, stop, position, reversed), plugs and lights, with a fake Tuya cloud."""

import asyncio

import pytest

from hub.adapters import tuya_adapter as ta
from hub.config import Secrets
from hub.models import HubError, StateIn


class FakeCloud:
    def __init__(self, devices=None, error=None):
        self.devices = devices if devices is not None else [
            {"id": "b1", "name": "Living Room Blinds", "category": "cl", "online": True, "product_name": "Curtain motor",
             "status": [{"code": "control", "value": "stop"}, {"code": "percent_state", "value": 40}, {"code": "work_state", "value": "closing"}]},
            {"id": "b2", "name": "Office Shade", "category": "cl", "online": True,
             "status": [{"code": "percent_control", "value": 100}]},
            {"id": "p1", "name": "Patio Plug", "category": "cz", "online": False, "status": [{"code": "switch_1", "value": True}]},
            {"id": "l1", "name": "Porch Light", "category": "dj", "online": True,
             "status": [{"code": "switch_led", "value": False}, {"code": "bright_value_v2", "value": 500}]},
            {"id": "x1", "name": "Weird sensor", "category": "wsdcg", "online": True, "status": []},
        ]
        self.error = error
        self.sent = []
        self.lists = 0

    def getdevices(self, verbose=False):
        self.lists += 1
        if self.error:
            return {"Error": "Error Response from Tuya Cloud", "Err": "911", "Payload": self.error}
        return {"result": self.devices, "success": True}

    def sendcommand(self, dev_id, commands):
        self.sent.append((dev_id, commands["commands"]))
        return {"success": True, "result": True}


def make(tmp_path, cloud=None):
    cloud = cloud or FakeCloud()
    a = ta.TuyaAdapter(Secrets(tmp_path / "s.json"), cloud_factory=lambda cfg: cloud)
    return a, cloud


@pytest.fixture(autouse=True)
def available(monkeypatch):
    monkeypatch.setattr(ta, "AVAILABLE", True)


def test_connect_lists_devices_and_keeps_only_the_project_keys(tmp_path):
    a, cloud = make(tmp_path)
    out = asyncio.run(a.connect("abcdefghij12", "0123456789abcdef0123", "us"))
    assert out == {"connected": True, "devices": 4}                # the sensor isn't shown
    assert a.secrets.get("tuya") == {"access_id": "abcdefghij12", "access_secret": "0123456789abcdef0123", "region": "us", "reversed": []}


def test_blinds_show_position_and_movement(tmp_path):
    a, _ = make(tmp_path)
    asyncio.run(a.connect("abcdefghij12", "0123456789abcdef0123", "us"))
    devs = {d.id: d for d in asyncio.run(a.devices())}
    b1, b2 = devs["tuya:b1"], devs["tuya:b2"]
    assert b1.kind == "blind" and b1.level == 40 and b1.on is True and b1.media == "Closing…"
    assert {"blind", "position", "onoff", "actions"} <= set(b1.capabilities)
    assert b2.media == "Open" and b2.level == 100
    assert devs["tuya:p1"].kind == "plug" and devs["tuya:p1"].online is False and devs["tuya:p1"].on is True
    light = devs["tuya:l1"]
    assert light.kind == "light" and light.on is False and light.brightness == 50 and "brightness" in light.capabilities
    assert "tuya:x1" not in devs


def test_blind_commands(tmp_path):
    a, cloud = make(tmp_path)
    asyncio.run(a.connect("abcdefghij12", "0123456789abcdef0123", "us"))
    asyncio.run(a.set_state("tuya:b1", StateIn(action="open")))
    asyncio.run(a.set_state("tuya:b1", StateIn(action="stop")))
    asyncio.run(a.set_state("tuya:b1", StateIn(on=False)))           # tile toggle / schedules: off = close
    asyncio.run(a.set_state("tuya:b1", StateIn(position=30)))
    assert cloud.sent == [("b1", [{"code": "control", "value": "open"}]), ("b1", [{"code": "control", "value": "stop"}]),
                          ("b1", [{"code": "control", "value": "close"}]), ("b1", [{"code": "percent_control", "value": 30}])]
    a.set_reversed("tuya:b1", True)                                   # a motor where 100 means closed
    asyncio.run(a.set_state("tuya:b1", StateIn(position=30)))
    assert cloud.sent[-1] == ("b1", [{"code": "percent_control", "value": 70}])
    a._at = float("-inf")
    assert {d.id: d.level for d in asyncio.run(a.devices())}["tuya:b1"] == 60
    with pytest.raises(HubError):
        asyncio.run(a.set_state("tuya:b1", StateIn(brightness=50)))


def test_plug_and_light_commands(tmp_path):
    a, cloud = make(tmp_path)
    asyncio.run(a.connect("abcdefghij12", "0123456789abcdef0123", "us"))
    asyncio.run(a.set_state("tuya:p1", StateIn(on=False)))
    asyncio.run(a.set_state("tuya:l1", StateIn(on=True, brightness=40)))
    assert cloud.sent == [("p1", [{"code": "switch_1", "value": False}]),
                          ("l1", [{"code": "switch_led", "value": True}, {"code": "bright_value_v2", "value": 400}])]


def test_errors_are_explained(tmp_path):
    a, _ = make(tmp_path, FakeCloud(error="sign invalid"))
    with pytest.raises(HubError) as err:
        asyncio.run(a.connect("abcdefghij12", "0123456789abcdef0123", "us"))
    assert err.value.status == 401 and "Access ID or Secret" in err.value.message and a.secrets.get("tuya") is None
    a, _ = make(tmp_path, FakeCloud(devices=[]))
    with pytest.raises(HubError) as err:
        asyncio.run(a.connect("abcdefghij12", "0123456789abcdef0123", "us"))
    assert err.value.status == 404 and "Link App Account" in err.value.message
    a, _ = make(tmp_path, FakeCloud(error="No permissions. Your subscription to cloud development plan has expired."))
    with pytest.raises(HubError) as err:
        asyncio.run(a.connect("abcdefghij12", "0123456789abcdef0123", "us"))
    assert err.value.status == 403 and "trial needs renewing" in err.value.message


def test_reading_is_cached_for_a_minute(tmp_path):
    a, cloud = make(tmp_path)
    asyncio.run(a.connect("abcdefghij12", "0123456789abcdef0123", "us"))
    for _ in range(3):
        asyncio.run(a.devices())
    assert cloud.lists == 1


def test_routes_and_room_all_off_leaves_blinds_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        assert client.post("/api/tuya/account", json={"access_id": "x", "access_secret": "y"}, headers=token).status_code == 422
        cloud = FakeCloud()
        hub.tuya._factory = lambda cfg: cloud
        r = client.post("/api/tuya/account", json={"access_id": "abcdefghij12", "access_secret": "0123456789abcdef0123"}, headers=token)
        assert r.json()["devices"] == 4
        devices = client.get("/api/devices", headers=token).json()["devices"]
        room = client.post("/api/rooms", json={"name": "Living"}, headers=token).json()["id"]
        for dev in ("tuya:b1", "tuya:l1"):
            client.patch(f"/api/devices/{dev}", json={"room_id": room}, headers=token)
        client.post(f"/api/rooms/{room}/state", json={"on": False}, headers=token)
        assert [s[0] for s in cloud.sent] == ["l1"]                  # the light went off, the blinds didn't close
        assert any(d["id"] == "tuya:b1" for d in devices)
