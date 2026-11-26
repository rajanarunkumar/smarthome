"""Govee and LG ThinQ accounts, with Govee's and LG's servers replaced by httpx mock transports."""

import asyncio
import json

import httpx
import pytest

from hub.adapters import govee_cloud
from hub.adapters.govee_adapter import GoveeAdapter
from hub.adapters.thinq_adapter import ThinQAdapter, power_key, region_of, summarize
from hub.config import Secrets
from hub.models import HubError, StateIn

LIGHT = {"sku": "H6008", "device": "AA:BB:CC:DD:EE:FF:00:11", "deviceName": "Porch bulb", "type": "devices.types.light",
         "capabilities": [
             {"type": "devices.capabilities.on_off", "instance": "powerSwitch"},
             {"type": "devices.capabilities.range", "instance": "brightness"},
             {"type": "devices.capabilities.color_setting", "instance": "colorRgb"},
             {"type": "devices.capabilities.color_setting", "instance": "colorTemperatureK",
              "parameters": {"range": {"min": 2700, "max": 6500}}}]}
PLUG = {"sku": "H5080", "device": "11:22:33:44:55:66:77:88", "deviceName": "Fan plug", "type": "devices.types.socket",
        "capabilities": [{"type": "devices.capabilities.on_off", "instance": "powerSwitch"}]}
SENSOR = {"sku": "H5179", "device": "99:99", "deviceName": "Thermometer", "type": "devices.types.thermometer"}


def govee_server(sent):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Govee-API-Key"] == "k" * 36
        if request.url.path.endswith("/user/devices"):
            return httpx.Response(200, json={"code": 200, "data": [LIGHT, PLUG, SENSOR]})
        body = json.loads(request.content)
        sent.append((request.url.path.rsplit("/", 1)[1], body["payload"]))
        if request.url.path.endswith("/device/state"):
            return httpx.Response(200, json={"code": 200, "payload": {"capabilities": [
                {"type": "devices.capabilities.online", "instance": "online", "state": {"value": True}},
                {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "state": {"value": 1}},
                {"type": "devices.capabilities.range", "instance": "brightness", "state": {"value": 55}},
                {"type": "devices.capabilities.color_setting", "instance": "colorRgb", "state": {"value": 0xFF8000}},
            ]}})
        return httpx.Response(200, json={"code": 200, "msg": "success"})
    return handler


@pytest.fixture
def govee(tmp_path, monkeypatch):
    sent = []
    real = httpx.AsyncClient
    monkeypatch.setattr(govee_cloud.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(govee_server(sent)), **kw))
    adapter = GoveeAdapter(Secrets(tmp_path / "secrets.json"))
    return adapter, sent


def test_govee_account_lists_lights_and_plugs_with_app_names(govee):
    adapter, sent = govee
    assert asyncio.run(adapter.connect_account("k" * 36)) == {"connected": True, "devices": 2}
    assert adapter.secrets.get("govee_api_key") == "k" * 36
    devices = {d.name: d for d in asyncio.run(adapter.devices())}
    assert set(devices) == {"Porch bulb", "Fan plug"}  # the thermometer can't be controlled, so it's left out
    bulb = devices["Porch bulb"]
    assert (bulb.on, bulb.brightness, bulb.color, bulb.online) == (True, 55, "#ff8000", True)
    assert (bulb.mirek_min, bulb.mirek_max) == (154, 370)
    assert devices["Fan plug"].kind == "plug" and devices["Fan plug"].capabilities == ["onoff"]


def test_govee_cloud_control_and_state_is_cached(govee):
    adapter, sent = govee
    asyncio.run(adapter.connect_account("k" * 36))
    asyncio.run(adapter.devices())
    asyncio.run(adapter.devices())  # second poll within REFRESH_SECONDS: no new state calls
    assert [s[0] for s in sent].count("state") == 2
    hid = govee_cloud.hub_id(LIGHT["device"])
    asyncio.run(adapter.set_state(hid, StateIn(on=False, color="#00ff00", mirek=200)))
    controls = [p["capability"] for c, p in sent if c == "control"]
    assert controls == [
        {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "value": 0},
        {"type": "devices.capabilities.color_setting", "instance": "colorRgb", "value": 0x00FF00},
        {"type": "devices.capabilities.color_setting", "instance": "colorTemperatureK", "value": 5000},
    ]


def test_govee_light_on_lan_is_controlled_locally(govee):
    adapter, sent = govee
    asyncio.run(adapter.connect_account("k" * 36))

    class Transport:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append(addr)
    adapter._transport = Transport()
    adapter._on_reply("10.0.0.5", json.dumps({"msg": {"cmd": "scan", "data": {
        "ip": "10.0.0.5", "device": LIGHT["device"], "sku": "H6008"}}}).encode())
    asyncio.run(adapter.set_state(govee_cloud.hub_id(LIGHT["device"]), StateIn(on=True)))
    assert adapter._transport.sent == [("10.0.0.5", 4003)]
    assert not [c for c, _ in sent if c == "control"]
    assert adapter.names()[govee_cloud.hub_id(LIGHT["device"])] == "Porch bulb"


def test_govee_bad_key(tmp_path, monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(govee_cloud.httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "Unauthorized"})), **kw))
    adapter = GoveeAdapter(Secrets(tmp_path / "secrets.json"))
    with pytest.raises(HubError) as err:
        asyncio.run(adapter.connect_account("x" * 36))
    assert err.value.status == 401 and adapter.cloud is None and not adapter.secrets.get("govee_api_key")


# LG ThinQ
WASHER = {"runState": {"currentState": "RUNNING"}, "timer": {"remainHour": 0, "remainMinute": 32},
          "operation": {"washerOperationMode": "POWER_OFF"}}
AC = {"operation": {"airConOperationMode": "POWER_ON"}, "airConJobMode": {"currentJobMode": "COOL"},
      "temperature": {"currentTemperature": 76, "targetTemperature": 72, "unit": "F"}}
FRIDGE = {"temperature": [{"targetTemperature": 37, "unit": "F", "locationName": "FRIDGE"},
                          {"targetTemperature": 0, "unit": "F", "locationName": "FREEZER"}],
          "doorStatus": [{"doorState": "OPEN", "locationName": "MAIN"}]}


def test_thinq_summaries():
    assert summarize(WASHER) == "Running · 32 min left"
    assert summarize(AC) == "Cool · 76° → 72°"
    assert summarize(FRIDGE) == "Fridge 37° · Freezer 0° · Door open"
    assert summarize(None) is None
    assert power_key(AC) == ("airConOperationMode", True)
    assert region_of("us") == "aic" and region_of("KR") == "kic" and region_of("DE") == "eic"


WASHER_PROFILE = {"property": {
    "operation": {"washerOperationMode": {"type": "enum", "mode": ["w"],
                                          "value": {"w": ["START", "STOP", "POWER_OFF", "WAKE_UP"]}}},
    "timer": {"relativeHourToStart": {"type": "range", "mode": ["r", "w"], "value": {"w": {"min": 3, "max": 19}}}},
}}


def thinq_server(sent, washer_status):
    devices = [
        {"deviceId": "w1", "deviceInfo": {"deviceType": "DEVICE_WASHER", "modelName": "WM4000", "alias": "Washer"}},
        {"deviceId": "a1", "deviceInfo": {"deviceType": "DEVICE_AIR_CONDITIONER", "modelName": "LW1022", "alias": "Bedroom AC"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api-aic.lgthinq.com"
        assert request.headers["Authorization"] == "Bearer " + "t" * 40
        assert request.headers["x-country"] == "US" and request.headers["x-api-key"]
        path = request.url.path
        if path == "/devices":
            return httpx.Response(200, json={"response": devices})
        if path.endswith("/profile"):
            return httpx.Response(200, json={"response": WASHER_PROFILE if "/w1/" in path else {}})
        if path.endswith("/state"):
            return httpx.Response(200, json={"response": washer_status if "/w1/" in path else AC})
        sent.append((path, request.headers.get("x-conditional-control"), json.loads(request.content)))
        return httpx.Response(200, json={"response": {}})
    return handler


def test_thinq_climate_power_and_laundry_safety(tmp_path, monkeypatch):
    from hub.adapters import thinq_adapter
    sent = []
    washer_status = {**WASHER, "remoteControlEnable": {"remoteControlEnabled": False}}
    real = httpx.AsyncClient
    monkeypatch.setattr(thinq_adapter.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(thinq_server(sent, washer_status)), **kw))
    adapter = ThinQAdapter(Secrets(tmp_path / "secrets.json"))

    async def run():
        assert await adapter.connect("t" * 40, "us") == {"connected": True, "devices": 2}
        devices = {d.name: d for d in await adapter.devices()}
        washer, ac = devices["Washer"], devices["Bedroom AC"]
        assert (washer.media, washer.ready, washer.delay_max) == ("Running · 32 min left", False, 19)
        assert [a["id"] for a in washer.actions] == ["START", "STOP", "POWER_OFF", "WAKE_UP"]
        assert (ac.on, ac.capabilities, ac.model) == (True, ["onoff"], "Air conditioner · LW1022")
        await adapter.set_state(ac.id, StateIn(on=False))

        # Remote Start is off at the machine: starting is refused, pausing and power off are fine.
        for blocked in (StateIn(action="START"), StateIn(delay_start_hours=5)):
            with pytest.raises(HubError) as err:
                await adapter.set_state(washer.id, blocked)
            assert err.value.status == 409 and "Remote Start" in err.value.message
        await adapter.set_state(washer.id, StateIn(action="STOP"))

        # Someone pressed Remote Start on the washer.
        washer_status["remoteControlEnable"]["remoteControlEnabled"] = True
        await adapter.set_state(washer.id, StateIn(action="START"))
        await adapter.set_state(washer.id, StateIn(delay_start_hours=5))
        with pytest.raises(HubError):  # outside this washer's 3-19 hour range
            await adapter.set_state(washer.id, StateIn(delay_start_hours=2))
        with pytest.raises(HubError):  # not a mode this washer accepts
            await adapter.set_state(washer.id, StateIn(action="RINSE"))
        await adapter.close()

    asyncio.run(run())
    assert adapter.secrets.get("thinq")["country"] == "US"
    assert sent == [
        ("/devices/a1/control", "true", {"operation": {"airConOperationMode": "POWER_OFF"}}),
        ("/devices/w1/control", "true", {"operation": {"washerOperationMode": "STOP"}}),
        ("/devices/w1/control", "true", {"operation": {"washerOperationMode": "START"}}),
        ("/devices/w1/control", "true", {"timer": {"relativeHourToStart": 5}}),
    ]
