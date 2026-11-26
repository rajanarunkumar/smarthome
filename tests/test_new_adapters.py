"""Sonos, LG TV and Govee adapters, tested with fakes instead of real devices."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from hub.adapters import govee_codec as codec
from hub.adapters.govee_adapter import GoveeAdapter
from hub.adapters.lgtv_adapter import current_source, magic_packet, sources_of
from hub.adapters.sonos_adapter import SonosAdapter
from hub.models import HubError, StateIn
from hub.store import Store


# Govee
def test_govee_messages():
    assert json.loads(codec.turn(True)) == {"msg": {"cmd": "turn", "data": {"value": 1}}}
    assert json.loads(codec.brightness(250))["msg"]["data"]["value"] == 100
    assert json.loads(codec.color("#ff8000"))["msg"]["data"]["color"] == {"r": 255, "g": 128, "b": 0}
    assert json.loads(codec.color_temp(370))["msg"]["data"]["colorTemInKelvin"] == 2703
    assert codec.mirek_to_kelvin(1000) == codec.KELVIN_MIN  # clamped to what Govee accepts


def test_govee_status_parsing():
    assert codec.parse(b"not json") is None
    cmd, data = codec.parse(b'{"msg":{"cmd":"devStatus","data":{"onOff":1,"brightness":40,'
                            b'"color":{"r":0,"g":0,"b":255},"colorTemInKelvin":0}}}')
    assert cmd == "devStatus"
    assert codec.status_to_state(data) == {"on": True, "brightness": 40, "color": "#0000ff"}
    warm = codec.status_to_state({"onOff": 0, "brightness": 10, "colorTemInKelvin": 2700})
    assert warm == {"on": False, "brightness": 10, "mirek": 370}


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((json.loads(data)["msg"]["cmd"], addr))


def test_govee_scan_reply_then_command():
    adapter = GoveeAdapter()
    adapter._transport = FakeTransport()
    adapter._on_reply("10.0.0.9", b'{"msg":{"cmd":"scan","data":{"ip":"10.0.0.9",'
                                  b'"device":"AB:CD:EF:01:02:03:04:05","sku":"H6159"}}}')
    assert adapter.names() == {"govee:abcdef0102030405": "Govee H6159"}
    asyncio.run(adapter.set_state("govee:abcdef0102030405", StateIn(on=True, brightness=30)))
    assert adapter._transport.sent == [("turn", ("10.0.0.9", 4003)), ("brightness", ("10.0.0.9", 4003))]
    with pytest.raises(HubError):
        asyncio.run(adapter.set_state("govee:nope", StateIn(on=True)))


# LG TV
def test_wake_on_lan_packet():
    packet = magic_packet("a8:23:fe:01:02:03")
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6
    assert packet[6:12] == bytes.fromhex("a823fe010203")
    with pytest.raises(ValueError):
        magic_packet("not-a-mac")


def test_lg_sources_and_current():
    state = SimpleNamespace(
        inputs={"HDMI_1": {"id": "HDMI_1", "label": "PS5", "appId": "com.webos.app.hdmi1"}},
        apps={"netflix": {"id": "netflix", "title": "Netflix"},
              "com.webos.app.hdmi1": {"id": "com.webos.app.hdmi1", "title": "HDMI 1"},
              "youtube.leanback.v4": {"id": "youtube.leanback.v4", "title": "YouTube"}},
        current_app_id="com.webos.app.hdmi1",
    )
    assert sources_of(state) == [
        {"id": "input:HDMI_1", "label": "PS5"},
        {"id": "app:netflix", "label": "Netflix"},
        {"id": "app:youtube.leanback.v4", "label": "YouTube"},
    ]
    assert current_source(state) == ("input:HDMI_1", "PS5")
    state.current_app_id = "netflix"
    assert current_source(state) == ("app:netflix", "Netflix")


# Sonos
class FakePlayer:
    def __init__(self):
        self.calls = []
        self.volume = 10
        self.mute = False
        self.group = SimpleNamespace(coordinator=self)

    def play(self):
        self.calls.append("play")

    def pause(self):
        self.calls.append("pause")

    def next(self):
        self.calls.append("next")


def test_sonos_switch_means_play_pause_and_volume():
    adapter = SonosAdapter()
    player = FakePlayer()
    adapter._players["sonos:rincon_1"] = player
    asyncio.run(adapter.set_state("sonos:rincon_1", StateIn(on=False, volume=35, muted=True)))
    asyncio.run(adapter.set_state("sonos:rincon_1", StateIn(playback="next")))
    assert player.calls == ["pause", "next"]
    assert (player.volume, player.mute) == (35, True)


def test_sonos_commands_go_to_group_coordinator():
    adapter = SonosAdapter()
    coordinator, member = FakePlayer(), FakePlayer()
    member.group = SimpleNamespace(coordinator=coordinator)
    adapter._players["sonos:member"] = member
    asyncio.run(adapter.set_state("sonos:member", StateIn(on=True)))
    assert coordinator.calls == ["play"] and member.calls == []


# Hub-kept names
def test_store_keeps_names_for_brands_without_rename(tmp_path):
    store = Store(tmp_path / "hub.db")
    store.set_device_name("govee:abc", " Desk strip ")
    store.set_device_name("govee:abc", "Desk light")
    assert store.device_names() == {"govee:abc": "Desk light"}
