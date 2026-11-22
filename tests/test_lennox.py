"""Lennox iComfort: connecting (account or local), reading zones, and commands, with a fake lennoxs30api."""

import asyncio
from types import SimpleNamespace

import pytest

from hub.adapters import lennox_adapter as lx
from hub.config import Secrets
from hub.models import HubError, StateIn


class Zone:
    def __init__(self, zid, name, active=True):
        self.id, self.name, self._active = zid, name, active
        self.temperature, self.humidity, self.systemMode, self.tempOperation = 71.0, 44.0, "heat", "heating"
        self.hsp, self.csp, self.fanMode = 68.0, 76.0, "auto"
        self.heatingOption = self.coolingOption = True
        self.calls = []

    def is_zone_active(self):
        return self._active

    async def setHVACMode(self, mode):
        self.calls.append(("mode", mode))
        self.systemMode = mode

    async def perform_setpoint(self, **kw):
        self.calls.append(("setpoint", {k: v for k, v in kw.items() if v is not None}))

    async def setFanMode(self, mode):
        self.calls.append(("fan", mode))


class Api:
    def __init__(self, zones, fail=None, **kw):
        self.kw, self.fail = kw, fail
        self.system = SimpleNamespace(sysId="sys-1", name="Home", zone_list=[], outdoorTemperature=48.0, productType="S30",
                                      single_setpoint_mode=False, cloud_status="online")
        self._zones = zones
        self.system_list, self.pumps, self.shut = [], 0, False

    async def serverConnect(self):
        if self.fail:
            raise self.fail
        self.system_list = [self.system]

    async def subscribe(self, system):
        pass

    async def messagePump(self):
        self.pumps += 1
        self.system.zone_list = self._zones   # config arrives with the first messages

    async def shutdown(self):
        self.shut = True


def make(tmp_path, zones, fail=None):
    made = []

    def factory(**kw):
        made.append(Api(zones, fail, **kw))
        return made[-1]
    return lx.LennoxAdapter(Secrets(tmp_path / "s.json"), factory=factory), made


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def available(monkeypatch):
    monkeypatch.setattr(lx, "AVAILABLE", True)


def test_connects_with_the_account_and_keeps_no_logs_of_it(tmp_path):
    zone = Zone(0, "Main")
    a, made = make(tmp_path, [zone, Zone(1, "Unused", active=False)])

    async def go():
        out = await a.connect("me@example.com", "pw", None)
        devs = await a.devices()
        await a.close()
        return out, devs
    out, devs = run(go())
    assert out == {"connected": True, "devices": 1}
    kw = made[0].kw
    assert kw["ip_address"] is None and kw["pii_message_logs"] is False and kw["message_debug_logging"] is False
    assert kw["app_id"].startswith("smarthomehub")
    assert a.secrets.get("lennox")["app_id"] == kw["app_id"]          # the same app id on every reconnect
    [d] = devs
    assert d.id == "lennox:sys-1:0" and d.kind == "thermostat" and d.name == "Home Thermostat" and d.on is True
    assert d.thermostat["heat"] == 68 and d.thermostat["cool"] == 76 and d.thermostat["modes"] == ["HEAT", "COOL", "HEATCOOL", "OFF"]
    assert d.media == "Heating to 68° · 71° inside" and {"label": "Outside", "value": "48°"} in d.details
    assert [x["id"] for x in d.actions] == ["fan_auto", "fan_on", "fan_circulate"] and "✓" in d.actions[0]["label"]


def test_local_connection_uses_the_ip_and_needs_no_password(tmp_path):
    a, made = make(tmp_path, [Zone(0, "Main")])

    async def go():
        await a.connect("", "", "192.168.1.50")
        await a.close()
    run(go())
    assert made[0].kw["ip_address"] == "192.168.1.50" and made[0].shut
    with pytest.raises(HubError) as err:
        run(a.connect("", "", None))
    assert err.value.status == 400


def test_a_rejected_login_is_reported_and_nothing_is_saved(tmp_path):
    bad = Exception("login failed")
    bad.error_code, bad.message = 1, "login failed response code [401]"
    a, made = make(tmp_path, [], fail=bad)
    with pytest.raises(HubError) as err:
        run(a.connect("me@example.com", "wrong", None))
    assert err.value.status == 401 and "email or password" in err.value.message
    assert a.secrets.get("lennox") is None and made[0].shut


def test_no_zones_means_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(lx, "CONNECT_TIMEOUT", 0.05)
    a, _ = make(tmp_path, [])
    with pytest.raises(HubError) as err:
        run(a.connect("", "", "192.168.1.50"))
    assert err.value.status == 404 and a.secrets.get("lennox") is None


def test_commands(tmp_path):
    zone = Zone(0, "Main")
    a, _ = make(tmp_path, [zone])

    async def go():
        await a.connect("me@example.com", "pw", None)
        await a.devices()
        await a.set_state("lennox:sys-1:0", StateIn(heat_f=70))
        await a.set_state("lennox:sys-1:0", StateIn(on=False))
        await a.devices()
        await a.set_state("lennox:sys-1:0", StateIn(on=True))                       # back to heat
        await a.set_state("lennox:sys-1:0", StateIn(thermostat_mode="HEATCOOL", heat_f=67, cool_f=77))
        await a.set_state("lennox:sys-1:0", StateIn(action="fan_circulate"))
        with pytest.raises(HubError):
            await a.set_state("lennox:sys-1:0", StateIn(action="fan_turbo"))
        await a.set_state("lennox:sys-1:0", StateIn(on=False))
        with pytest.raises(HubError) as err:
            await a.set_state("lennox:sys-1:0", StateIn(heat_f=70))
        assert err.value.status == 409
        await a.close()
    run(go())
    assert zone.calls == [("setpoint", {"r_hsp": 70}), ("mode", "off"), ("mode", "heat"), ("mode", "heat and cool"),
                          ("setpoint", {"r_hsp": 67, "r_csp": 77}), ("fan", "circulate"), ("mode", "off")]


def test_a_cooling_only_zone_refuses_heat(tmp_path):
    zone = Zone(0, "Main")
    zone.heatingOption, zone.systemMode = False, "cool"
    a, _ = make(tmp_path, [zone])

    async def go():
        await a.connect("me@example.com", "pw", None)
        with pytest.raises(HubError) as err:
            await a.set_state("lennox:sys-1:0", StateIn(thermostat_mode="HEAT"))
        await a.close()
        return err.value
    assert "doesn't offer Heat" in run(go()).message


def test_the_background_loop_reconnects_after_a_drop(tmp_path, monkeypatch):
    zone = Zone(0, "Main")
    a, made = make(tmp_path, [zone])
    sleeps = []
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        sleeps.append(s)
        await real_sleep(0)
    monkeypatch.setattr(lx.asyncio, "sleep", fast_sleep)

    async def go():
        await a.connect("", "", "192.168.1.50")
        a._task.cancel()
        first = made[0]

        async def boom():
            raise ConnectionResetError("dropped")
        first.messagePump = boom
        a._task = asyncio.create_task(a._run())
        for _ in range(50):
            await real_sleep(0)
            if len(made) > 1 and a._api is made[-1]:
                break
        await a.close()
        return first
    first = run(go())
    assert first.shut and len(made) >= 2 and 30 in sleeps   # dropped connection closed, waited, reconnected


def test_lennox_route_validates_the_ip(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        assert client.post("/api/lennox/account", json={"ip": "thermostat.local"}, headers=token).status_code == 422
        assert client.get("/api/status", headers=token).json()["lennox"]["connected"] is False
