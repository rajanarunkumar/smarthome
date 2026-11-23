"""Ring (doorbell + cameras, two-step sign-in) and Nest (Google SDM thermostat), with fake services."""

import base64
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from hub.adapters import nest_adapter, ring_adapter
from hub.adapters import rainbird_adapter as rb
from hub.config import Secrets
from hub.models import HubError, StateIn

# ---------------- Ring ----------------


class FakeCam:
    def __init__(self, api_id, family, name, motion=True, lights=None, siren=None, battery=None):
        self.device_api_id, self.family, self.name, self.model = api_id, family, name, "Fake"
        self.motion_detection, self.lights, self.siren, self.battery_life = motion, lights or "", siren or 0, battery
        self._caps = {"motion_detection"} | ({"light"} if lights else set()) | ({"siren"} if siren is not None else set()) \
            | ({"battery"} if battery is not None else set())
        self.wifi_signal_strength, self.wifi_signal_category, self.firmware = -52, "good", "1.2.3"
        self.connection_status = "online"
        self.calls = []
        self.image = b"\xff\xd8jpeg"

    def has_capability(self, name):
        return name in self._caps

    async def async_history(self, limit=30):
        return [{"kind": "ding", "created_at": datetime(2026, 11, 20, 21, 5, tzinfo=timezone.utc), "answered": False},
                {"kind": "motion", "created_at": datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)}][:limit]

    async def async_set_motion_detection(self, on):
        self.calls.append(("motion", on))

    async def async_set_lights(self, state):
        self.calls.append(("lights", state))

    async def async_set_siren(self, seconds):
        self.calls.append(("siren", seconds))

    async def async_get_snapshot(self):
        return self.image


def fake_ring(monkeypatch, cams, needs_code=True, good_code="123456"):
    seen = {"fetch": [], "closed": 0}

    class Auth:
        def __init__(self, ua, token=None, updater=None, hardware_id=None):
            self.token, self.updater, self.hw = token, updater, hardware_id or "hw-1"

        async def async_fetch_token(self, email, password, code=None):
            seen["fetch"].append((email, password, code))
            if password != "pw":
                raise ring_adapter.AuthenticationError("bad")
            if needs_code and code is None:
                raise ring_adapter.Requires2FAError("2fa")
            if needs_code and code != good_code:
                raise ring_adapter.AuthenticationError("bad code")
            self.token = {"access_token": "a", "refresh_token": "r"}
            return self.token

        def get_hardware_id(self):
            return self.hw

        async def async_close(self):
            seen["closed"] += 1

    class Ring:
        def __init__(self, auth):
            self.auth = auth

        async def async_update_data(self):
            if self.auth.token is None:
                raise ring_adapter.AuthenticationError("no token")
            self.auth.updater({"access_token": "a2", "refresh_token": "r2"})   # Ring refreshes tokens as it goes

        def devices(self):
            return SimpleNamespace(doorbots=[c for c in cams if c.family == "doorbots"], authorized_doorbots=[],
                                   stickup_cams=[c for c in cams if c.family == "stickup_cams"])

    monkeypatch.setattr(ring_adapter, "AVAILABLE", True)
    monkeypatch.setattr(ring_adapter, "Auth", Auth, raising=False)
    monkeypatch.setattr(ring_adapter, "Ring", Ring, raising=False)
    return seen


def test_ring_signs_in_with_a_code_and_keeps_only_the_token(tmp_path, monkeypatch):
    door = FakeCam(11, "doorbots", "Front Door", battery=80)
    seen = fake_ring(monkeypatch, [door])
    a = ring_adapter.RingAdapter(Secrets(tmp_path / "s.json"))
    with pytest.raises(HubError) as err:
        asyncio.run(a.connect("", "", "123456"))          # a code before any password
    assert err.value.status == 409
    assert asyncio.run(a.connect("me@example.com", "pw")) == {"needs_code": True}
    assert a.secrets.get("ring") is None                  # nothing saved yet, and never the password
    with pytest.raises(HubError) as err:
        asyncio.run(a.connect("", "", "999999"))
    assert err.value.status == 401 and "code" in err.value.message
    asyncio.run(a.connect("me@example.com", "pw"))        # start again after a wrong code
    out = asyncio.run(a.connect("", "", "123456"))
    assert out == {"connected": True, "devices": 1}
    saved = a.secrets.get("ring")
    assert saved["hardware_id"] == "hw-1" and saved["token"]["refresh_token"] == "r2"   # refreshed token was saved
    assert "pw" not in json.dumps(saved)
    assert seen["fetch"][-1] == ("me@example.com", "pw", "123456")


def test_ring_wrong_password_is_reported(tmp_path, monkeypatch):
    fake_ring(monkeypatch, [])
    a = ring_adapter.RingAdapter(Secrets(tmp_path / "s.json"))
    with pytest.raises(HubError) as err:
        asyncio.run(a.connect("me@example.com", "nope"))
    assert err.value.status == 401 and "email or password" in err.value.message


def connected_ring(tmp_path, monkeypatch, cams):
    fake_ring(monkeypatch, cams, needs_code=False)
    a = ring_adapter.RingAdapter(Secrets(tmp_path / "s.json"))
    asyncio.run(a.connect("me@example.com", "pw"))
    return a


def test_ring_devices_show_the_latest_events_and_controls(tmp_path, monkeypatch):
    door = FakeCam(11, "doorbots", "Front Door", battery=18)
    indoor = FakeCam(22, "stickup_cams", "Living Room", motion=False, siren=0)
    a = connected_ring(tmp_path, monkeypatch, [door, indoor])
    devs = {d.id: d for d in asyncio.run(a.devices())}
    d, cam = devs["ring:11"], devs["ring:22"]
    assert d.kind == "doorbell" and cam.kind == "camera" and d.battery == 18 and cam.battery is None
    assert d.media.startswith("Doorbell rang") and {x["label"] for x in d.details} >= {"Last ring", "Last motion", "Wi‑Fi signal"}
    assert "Last ring" not in {x["label"] for x in cam.details}
    assert {"events", "snapshot", "actions"} <= set(cam.capabilities)
    assert [x["id"] for x in cam.actions] == ["motion", "siren"] and "off" in cam.actions[0]["label"]

    asyncio.run(a.set_state("ring:22", StateIn(action="motion")))
    asyncio.run(a.set_state("ring:22", StateIn(action="siren")))
    assert indoor.calls == [("motion", True), ("siren", ring_adapter.SIREN_SECONDS)]
    with pytest.raises(HubError):
        asyncio.run(a.set_state("ring:11", StateIn(action="lights")))   # the doorbell has no light
    with pytest.raises(HubError) as err:
        asyncio.run(a.set_state("ring:99", StateIn(action="motion")))
    assert err.value.status == 404


def test_ring_history_and_snapshot(tmp_path, monkeypatch):
    door = FakeCam(11, "doorbots", "Front Door")
    a = connected_ring(tmp_path, monkeypatch, [door])
    events = asyncio.run(a.history("ring:11", fresh=True))
    assert [e["label"] for e in events] == ["Doorbell rang", "Motion"] and events[0]["answered"] is False and events[0]["when"]
    assert asyncio.run(a.snapshot("ring:11")).startswith(b"\xff\xd8")
    door.image = None
    with pytest.raises(HubError) as err:
        asyncio.run(a.snapshot("ring:11"))
    assert err.value.status == 404 and "Snapshot Capture" in err.value.message


def test_ring_signed_out_by_ring_says_so(tmp_path, monkeypatch):
    a = connected_ring(tmp_path, monkeypatch, [])
    a._ring.auth.token = None
    a._data_at = 0
    with pytest.raises(HubError) as err:
        asyncio.run(a.devices())
    assert err.value.status == 401 and "Connect again" in a.error


# ---------------- Nest ----------------
PROJECT = "proj-1234-abcd"
DEVICE = f"enterprises/{PROJECT}/devices/dev1"


def thermostat(mode="HEAT", hvac="HEATING", heat=20.0, cool=None, eco="OFF", online="ONLINE"):
    setpoint = {k: v for k, v in (("heatCelsius", heat), ("coolCelsius", cool)) if v is not None}
    return {"name": DEVICE, "type": "sdm.devices.types.THERMOSTAT",
            "parentRelations": [{"displayName": "Hallway"}],
            "traits": {"sdm.devices.traits.Info": {"customName": ""},
                       "sdm.devices.traits.Temperature": {"ambientTemperatureCelsius": 21.1},
                       "sdm.devices.traits.Humidity": {"ambientHumidityPercent": 41},
                       "sdm.devices.traits.ThermostatMode": {"mode": mode, "availableModes": ["HEAT", "COOL", "HEATCOOL", "OFF"]},
                       "sdm.devices.traits.ThermostatHvac": {"status": hvac},
                       "sdm.devices.traits.ThermostatEco": {"mode": eco, "heatCelsius": 15.5, "coolCelsius": 26.6},
                       "sdm.devices.traits.ThermostatTemperatureSetpoint": setpoint,
                       "sdm.devices.traits.Fan": {"timerMode": "OFF"},
                       "sdm.devices.traits.Connectivity": {"status": online}}}


class Google:
    def __init__(self):
        self.devices = [thermostat()]
        self.commands, self.token_posts = [], []
        self.refresh_error = None
        self.command_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            self.token_posts.append(form)
            if form.get("grant_type") == ["refresh_token"] and self.refresh_error:
                return httpx.Response(400, json={"error": self.refresh_error})
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3599, "refresh_token": "rt"})
        assert request.headers["authorization"] == "Bearer at"
        if request.method == "GET" and request.url.path == f"/v1/enterprises/{PROJECT}/devices":
            return httpx.Response(200, json={"devices": self.devices + [{"name": "x/cam", "type": "sdm.devices.types.CAMERA", "traits": {}}]})
        if request.method == "POST" and request.url.path == f"/v1/{DEVICE}:executeCommand":
            if self.command_status != 200:
                return httpx.Response(self.command_status, json={"error": {"message": "Rate limited"}})
            self.commands.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": {"message": "unknown"}})


def nest(tmp_path, google, linked=True):
    a = nest_adapter.NestAdapter(Secrets(tmp_path / "s.json"), transport=httpx.MockTransport(google))
    a.configure(PROJECT, "client-123.apps.googleusercontent.com", "secret-xyz")
    if linked:
        a.secrets.set("nest", {**a.config, "refresh_token": "rt"})
    return a


def test_nest_link_flow_exchanges_the_code_and_keeps_the_refresh_token(tmp_path):
    g = Google()
    a = nest(tmp_path, g, linked=False)
    origin = "https://hub.example.ts.net"
    url = urlparse(a.link_url(origin))
    q = parse_qs(url.query)
    assert url.netloc == "nestservices.google.com" and PROJECT in url.path
    assert q["redirect_uri"] == [origin + "/"] and q["access_type"] == ["offline"] and q["scope"] == [nest_adapter.SCOPE]
    state = q["state"][0]
    assert state.startswith("nest.")
    with pytest.raises(HubError):
        asyncio.run(a.finish_link("https://other.example", "code-abcdefgh", state))   # started on a different address
    state = parse_qs(urlparse(a.link_url(origin)).query)["state"][0]
    out = asyncio.run(a.finish_link(origin, "code-abcdefgh", state))
    assert out == {"connected": True, "devices": 1}                     # the camera isn't a thermostat
    assert a.config["refresh_token"] == "rt" and g.token_posts[0]["redirect_uri"] == [origin + "/"]
    with pytest.raises(HubError) as err:
        asyncio.run(a.finish_link(origin, "code-abcdefgh", state))      # a state works once
    assert "expired or was already used" in err.value.message


def test_nest_thermostat_reads_in_fahrenheit(tmp_path):
    g = Google()
    a = nest(tmp_path, g)
    [d] = asyncio.run(a.devices())
    assert d.id == "nest:dev1" and d.kind == "thermostat" and d.name == "Hallway Thermostat" and d.on is True
    t = d.thermostat
    assert (t["current"], t["heat"], t["humidity"], t["mode"], t["eco"]) == (70, 68, 41, "HEAT", False)
    assert d.media == "Heating to 68° · 70° inside"
    g.devices = [thermostat(mode="HEATCOOL", hvac="OFF", heat=20.0, cool=25.5)]
    a._devices_at = 0
    [d] = asyncio.run(a.devices())
    assert d.on is False and d.media == "Holding 68–78° · 70° inside"
    g.devices = [thermostat(eco="MANUAL_ECO")]
    a._devices_at = 0
    [d] = asyncio.run(a.devices())
    assert d.thermostat["eco"] is True and d.thermostat["heat"] == 60 and d.media.startswith("Eco")


def test_nest_commands(tmp_path):
    g = Google()
    a = nest(tmp_path, g)
    asyncio.run(a.devices())
    asyncio.run(a.set_state("nest:dev1", StateIn(heat_f=72)))
    assert g.commands[-1] == {"command": "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat", "params": {"heatCelsius": 22.22}}
    asyncio.run(a.set_state("nest:dev1", StateIn(on=False)))
    assert g.commands[-1]["params"] == {"mode": "OFF"}
    g.devices = [thermostat(mode="OFF", hvac="OFF", heat=None)]
    asyncio.run(a.set_state("nest:dev1", StateIn(on=True)))                 # back to the mode it was in
    assert g.commands[-1]["params"] == {"mode": "HEAT"}
    with pytest.raises(HubError) as err:
        asyncio.run(a.set_state("nest:dev1", StateIn(heat_f=70)))            # off: no setpoint
    assert err.value.status == 409
    g.devices = [thermostat(mode="HEATCOOL", hvac="OFF", heat=20.0, cool=25.0)]
    a._devices_at = 0
    with pytest.raises(HubError):
        asyncio.run(a.set_state("nest:dev1", StateIn(heat_f=76)))            # 76 heat vs 77 cool: too close
    asyncio.run(a.set_state("nest:dev1", StateIn(cool_f=80)))
    assert g.commands[-1] == {"command": "sdm.devices.commands.ThermostatTemperatureSetpoint.SetRange",
                              "params": {"heatCelsius": 20.0, "coolCelsius": 26.67}}
    g.devices = [thermostat(eco="MANUAL_ECO")]
    a._devices_at = 0
    with pytest.raises(HubError) as err:
        asyncio.run(a.set_state("nest:dev1", StateIn(heat_f=70)))
    assert "Eco" in err.value.message
    asyncio.run(a.set_state("nest:dev1", StateIn(eco=False, heat_f=70)))    # Eco off and a temperature in one go
    assert [c["command"].rsplit(".", 1)[-1] for c in g.commands[-2:]] == ["SetMode", "SetHeat"]
    asyncio.run(a.set_state("nest:dev1", StateIn(fan_minutes=15)))
    assert g.commands[-1]["params"] == {"timerMode": "ON", "duration": "900s"}


def test_nest_errors_are_plain(tmp_path):
    g = Google()
    a = nest(tmp_path, g)
    asyncio.run(a.devices())
    g.command_status = 429
    with pytest.raises(HubError) as err:
        asyncio.run(a.set_state("nest:dev1", StateIn(heat_f=70)))
    assert err.value.status == 429 and "Wait a minute" in err.value.message
    gb = Google()
    gb.refresh_error = "invalid_grant"
    (tmp_path / "b").mkdir()
    b = nest(tmp_path / "b", gb)
    with pytest.raises(HubError) as err:
        asyncio.run(b.devices())
    assert err.value.status == 401 and "Link the Google account again" in err.value.message and b.error


def test_thermostats_count_in_energy_tracking():
    from hub.energy import TRACKED_KINDS
    assert "thermostat" in TRACKED_KINDS


# ---------------- routes ----------------
def test_routes_validate_and_reach_the_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        assert client.post("/api/ring/account", json={}, headers=token).status_code == 400
        assert client.post("/api/nest/code", json={"code": "abcdefghijk", "state": "google.xyz12345"}, headers=token).status_code == 422
        assert client.post("/api/nest/link", headers=token).status_code in (403, 409)   # not configured (or no browser origin)
        status = client.get("/api/status", headers=token).json()
        assert status["ring"]["connected"] is False and status["nest"]["connected"] is False
        stored = client.get("/api/storage", headers=token)
        assert stored.status_code == 200


# ---------------- Rain Bird library check ----------------
def test_rainbird_library_version_check():
    assert rb.library_too_old("6.0.5") and not rb.library_too_old("6.1.0") and not rb.library_too_old("6.10.0")
    assert not rb.library_too_old(None)


@pytest.mark.skipif(not rb.AVAILABLE, reason="pyrainbird needs Python 3.13")
def test_rainbird_test_warns_about_an_old_library(tmp_path, monkeypatch):
    async def open_connection(host, port):
        return None, SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    monkeypatch.setattr(rb, "library_version", lambda: "6.0.5")

    async def refused(session, host, password):
        raise ConnectionRefusedError()
    monkeypatch.setattr(rb, "create_controller", refused)
    steps = asyncio.run(rb.RainBirdAdapter(Secrets(tmp_path / "s.json")).diagnose("192.168.1.40", "pw"))
    assert any("too old" in s["title"] for s in steps)


# ---------------- Nest events (Pub/Sub) ----------------
SUB = "projects/thorntree-smarthome/subscriptions/nest-events-sub"


def event_message(traits, name=DEVICE, ack="a1"):
    payload = {"timestamp": "2026-11-22T18:40:00Z", "resourceUpdate": {"name": name, "traits": traits}}
    return {"ackId": ack, "message": {"data": base64.b64encode(json.dumps(payload).encode()).decode()}}


class GoogleWithPubSub(Google):
    def __init__(self):
        super().__init__()
        self.queue, self.acked, self.pull_status = [], [], 200

    def __call__(self, request):
        if request.url.host == "pubsub.googleapis.com":
            assert request.headers["authorization"] == "Bearer at"
            if request.url.path == f"/v1/{SUB}:pull":
                if self.pull_status != 200:
                    return httpx.Response(self.pull_status, json={"error": {"message": "denied"}})
                msgs, self.queue = self.queue, []
                return httpx.Response(200, json={"receivedMessages": msgs} if msgs else {})
            if request.url.path == f"/v1/{SUB}:acknowledge":
                self.acked += json.loads(request.content)["ackIds"]
                return httpx.Response(200, json={})
        return super().__call__(request)


def nest_events(tmp_path, google):
    a = nest_adapter.NestAdapter(Secrets(tmp_path / "s.json"), transport=httpx.MockTransport(google))
    a.configure(PROJECT, "client-123.apps.googleusercontent.com", "secret-xyz", SUB)
    a.secrets.set("nest", {**a.config, "refresh_token": "rt"})
    return a


def test_nest_link_asks_for_pubsub_only_when_events_are_configured(tmp_path):
    (tmp_path / "p").mkdir(); (tmp_path / "e").mkdir()
    plain = nest(tmp_path / "p", Google(), linked=False)
    assert parse_qs(urlparse(plain.link_url("https://hub.example.ts.net")).query)["scope"] == [nest_adapter.SCOPE]
    ev = nest_events(tmp_path / "e", GoogleWithPubSub())
    scope = parse_qs(urlparse(ev.link_url("https://hub.example.ts.net")).query)["scope"][0]
    assert scope.split() == [nest_adapter.SCOPE, nest_adapter.PUBSUB_SCOPE]


def test_nest_events_update_the_thermostat_and_are_acknowledged(tmp_path):
    g = GoogleWithPubSub()
    a = nest_events(tmp_path, g)
    recorded = []
    a.on_update = lambda i, n, th: recorded.append((i, n, th["hvac"], th["current"]))

    async def run():
        await a._list(force=True)
        g.queue = [event_message({"sdm.devices.traits.ThermostatHvac": {"status": "OFF"},
                                  "sdm.devices.traits.Temperature": {"ambientTemperatureCelsius": 22.2}})]
        assert await a.pull_events() == 1
        assert await a.pull_events() == 0
        return await a.devices()

    (d,) = asyncio.run(run())
    assert g.acked == ["a1"]
    assert d.thermostat["hvac"] == "OFF" and d.thermostat["current"] == 72 and d.on is False   # no extra GET needed
    assert recorded == [("nest:dev1", "Hallway Thermostat", "OFF", 72)]
    (ev,) = a.recent_events()
    assert ev["changed"] == ["Temperature", "ThermostatHvac"] and ev["t"] == "2026-11-22T18:40:00Z"
    assert asyncio.run(a.status())["last_event_at"] == "2026-11-22T18:40:00Z"


def test_nest_event_for_an_unknown_device_rereads_the_list(tmp_path):
    a = nest_events(tmp_path, GoogleWithPubSub())
    asyncio.run(a._list(force=True))
    assert a.apply_event({"resourceUpdate": {"name": "enterprises/x/devices/new", "traits": {}}}) is None
    assert a._devices_at == 0.0


def test_nest_events_permission_error_is_plain(tmp_path):
    g = GoogleWithPubSub()
    g.pull_status = 403
    a = nest_events(tmp_path, g)
    with pytest.raises(HubError) as err:
        asyncio.run(a.pull_events())
    assert "Link again" in err.value.message and "Subscriber" in err.value.message
