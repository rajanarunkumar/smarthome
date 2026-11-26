"""Dyson, Ecovacs and Pentair, tested with fakes standing in for the devices and libraries."""

import asyncio
from types import SimpleNamespace

import pytest

from hub.adapters import dyson_adapter, ecovacs_adapter, pentair_adapter
from hub.adapters.dyson_adapter import DysonAdapter, kelvin_to_f, readings, summary
from hub.adapters.ecovacs_adapter import EcovacsAdapter, actions_for
from hub.adapters.pentair_adapter import PentairAdapter, details_of, is_running
from hub.config import Secrets
from hub.models import HubError, StateIn


# Dyson
class FakeDyson:
    serial = "AB1-US-ABC1234A"
    is_connected = True

    def __init__(self):
        self.is_on, self.auto_mode, self.speed, self.oscillation, self.night_mode = True, False, 4, True, False
        self.temperature, self.humidity, self.particulate_matter_2_5 = 295.0, 41, 6
        self.hepa_filter_life, self.carbon_filter_life = 80, -1
        self.calls = []

    def turn_off(self): self.calls.append("off"); self.is_on = False
    def turn_on(self): self.calls.append("on"); self.is_on = True
    def set_speed(self, s): self.calls.append(f"speed {s}"); self.speed, self.auto_mode = s, False
    def enable_auto_mode(self): self.calls.append("auto on"); self.auto_mode = True
    def disable_auto_mode(self): self.calls.append("auto off"); self.auto_mode = False
    def enable_oscillation(self): self.calls.append("osc on")
    def disable_oscillation(self): self.calls.append("osc off"); self.oscillation = False
    def enable_night_mode(self): self.calls.append("night on")
    def disable_night_mode(self): self.calls.append("night off")


def test_dyson_readings_and_summary():
    dev = FakeDyson()
    assert kelvin_to_f(295.0) == 71 and kelvin_to_f(-1) is None
    assert summary(dev) == "Speed 4 · PM2.5 6 · 71°F"
    labels = [r["label"] for r in readings(dev)]
    assert labels == ["Temperature", "Humidity", "PM2.5", "HEPA filter"]  # no carbon filter on this model


def test_dyson_controls(tmp_path):
    adapter = DysonAdapter(Secrets(tmp_path / "s.json"))
    adapter.secrets.set("dyson", {"devices": [{"serial": FakeDyson.serial, "credential": "x",
                                                "device_type": "438", "name": "Bedroom purifier"}]})
    dev = FakeDyson()
    hid = "dyson:" + dev.serial.lower()
    adapter._devs[hid] = dev
    [d] = asyncio.run(adapter.devices())
    assert (d.name, d.on, d.level, d.level_max) == ("Bedroom purifier", True, 4, 10)
    assert [a["label"] for a in d.actions] == ["Auto: off", "Oscillation: on", "Night mode: off"]
    asyncio.run(adapter.set_state(hid, StateIn(level=7)))
    asyncio.run(adapter.set_state(hid, StateIn(action="auto")))
    asyncio.run(adapter.set_state(hid, StateIn(action="oscillation")))
    asyncio.run(adapter.set_state(hid, StateIn(on=False)))
    assert dev.calls == ["speed 7", "auto on", "osc off", "off"]
    with pytest.raises(HubError):
        asyncio.run(adapter.set_state(hid, StateIn(action="turbo")))


def test_dyson_code_must_be_requested_first(tmp_path):
    adapter = DysonAdapter(Secrets(tmp_path / "s.json"))
    with pytest.raises(HubError) as err:
        asyncio.run(adapter.verify_code("123456", "pw"))
    assert err.value.status == 409


def test_dyson_device_not_seen_yet_is_offline(tmp_path):
    adapter = DysonAdapter(Secrets(tmp_path / "s.json"))
    adapter.secrets.set("dyson", {"devices": [{"serial": "X1", "credential": "c", "device_type": "438", "name": "Den fan"}]})
    [d] = asyncio.run(adapter.devices())
    assert (d.online, d.media) == (False, "Looking for it on the network…")


# Ecovacs
def test_ecovacs_buttons_follow_state():
    ids = lambda state: [a["id"] for a in actions_for(state)]
    assert ids("DOCKED") == ["clean"] and ids("CLEANING") == ["pause", "dock"]
    assert ids("PAUSED") == ["resume", "dock"] and ids(None) == ["clean"]


@pytest.mark.skipif(not ecovacs_adapter.AVAILABLE, reason="deebot-client needs Python 3.13")
def test_ecovacs_commands(tmp_path):
    from deebot_client.commands.json.clean import CleanAction
    sent = []

    class Bot:
        device_info = {"did": "E01", "nick": "Downstairs robot", "deviceName": "DEEBOT T20"}
        capabilities = SimpleNamespace(
            charge=SimpleNamespace(execute=lambda: "charge"),
            clean=SimpleNamespace(action=SimpleNamespace(command=lambda a: f"clean {a.value}")))

        async def execute_command(self, cmd):
            sent.append(cmd)

    adapter = EcovacsAdapter(Secrets(tmp_path / "s.json"))
    adapter._bots["ecovacs:e01"] = Bot()
    adapter._state["ecovacs:e01"] = {"state": "PAUSED", "battery": 64}
    [d] = asyncio.run(adapter.devices())
    assert (d.name, d.media, d.battery, d.on) == ("Downstairs robot", "Paused", 64, False)
    asyncio.run(adapter.set_state("ecovacs:e01", StateIn(on=True)))       # paused + switch on = resume
    asyncio.run(adapter.set_state("ecovacs:e01", StateIn(action="dock")))
    asyncio.run(adapter.set_state("ecovacs:e01", StateIn(action="pause")))
    assert sent == [f"clean {CleanAction.RESUME.value}", "charge", f"clean {CleanAction.PAUSE.value}"]


# Pentair
PUMP = {"deviceId": "p1", "deviceType": "IF31", "productInfo": {"nickName": "Pool pump", "maker": "Pentair", "model": "IntelliFlo VSF"},
        "fields": {"s25": {"name": "Pump enabled", "value": "1", "category": "data"},
                   "s19": {"name": "Speed", "unit": "RPM", "value": 2400, "category": "data"},
                   "s11": {"name": "Security key", "value": "secret", "category": "data"},
                   "s30": {"name": "Firmware build", "value": "7", "category": "diagnostic"}}}


def test_pentair_details_hide_secrets():
    assert details_of(PUMP) == [{"label": "Pump enabled", "value": "Yes"}, {"label": "Speed", "value": "2400 RPM"}]
    assert is_running(PUMP) is True and is_running({"deviceType": "PPA0", "fields": {}}) is None


def test_pentair_pump_on_off(tmp_path, monkeypatch):
    sent = []

    class FakePentair:
        def __init__(self, **kw): pass
        def get_devices(self): return {"data": [{"deviceId": "p1"}]}
        def get_device(self, device_id): return {"data": PUMP}
        def get_tokens(self): return {"access_token": "a", "id_token": "i", "refresh_token": "r2"}

        def _Pentair__request(self, method, path, *, path_params, json):
            sent.append((method, path.format(**path_params), json))

    monkeypatch.setattr(pentair_adapter, "Pentair", FakePentair)
    adapter = PentairAdapter(Secrets(tmp_path / "s.json"))
    adapter.secrets.set("pentair", {"email": "me@example.com", "refresh_token": "r1"})
    [d] = asyncio.run(adapter.devices())
    assert (d.name, d.kind, d.on, d.media, d.model) == ("Pool pump", "pool", True, "Running", "Pentair IntelliFlo VSF")
    assert adapter.secrets.get("pentair")["refresh_token"] == "r2"  # renewed tokens are kept
    assert d.capabilities == ["onoff"]
    asyncio.run(adapter.set_state(d.id, StateIn(on=False)))
    assert sent == [("put", "device/device-service/user/device/p1", {"payload": {"d25": "0"}})]
    with pytest.raises(HubError):  # only on/off
        asyncio.run(adapter.set_state(d.id, StateIn(brightness=50)))
    assert "s11" not in adapter.raw_fields()[0]["fields"]  # the security key never leaves the hub


def test_dyson_sticker_setup_needs_no_account(tmp_path):
    adapter = DysonAdapter(Secrets(tmp_path / "s.json"))
    # A made-up sticker: Wi-Fi name DYSON-<serial>-<product code>, plus its Wi-Fi password.
    out = asyncio.run(adapter.add_from_sticker("DYSON-AB1-US-ABC1234A-438", "SomeWifiPassword1234", "Bedroom purifier"))
    assert out == {"connected": True, "devices": 1, "name": "Bedroom purifier"}
    [saved] = adapter.secrets.get("dyson")["devices"]
    assert saved["serial"] == "AB1-US-ABC1234A" and saved["name"] == "Bedroom purifier"
    assert saved["device_type"] == "438" and saved["credential"] and "SomeWifiPassword1234" not in saved["credential"]
    # Adding the same device again replaces it, a second device is kept alongside.
    asyncio.run(adapter.add_from_sticker("DYSON-AB1-US-ABC1234A-438", "SomeWifiPassword1234", "Office purifier"))
    asyncio.run(adapter.add_from_sticker("DYSON-XY9-US-ZZZ9999B-475", "AnotherPassword5678"))
    names = {d["serial"]: d["name"] for d in adapter.secrets.get("dyson")["devices"]}
    assert names["AB1-US-ABC1234A"] == "Office purifier" and len(names) == 2
    with pytest.raises(HubError) as err:
        asyncio.run(adapter.add_from_sticker("my-home-wifi", "SomeWifiPassword1234"))
    assert err.value.status == 400 and "DYSON-" in err.value.message
