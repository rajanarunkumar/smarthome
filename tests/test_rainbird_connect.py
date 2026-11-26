"""Rain Bird connectivity: plain-language errors, no retrying a rejected password, backoff, one request at a time,
and a step-by-step connection test."""

import asyncio
from types import SimpleNamespace

import pytest

from hub.adapters import rainbird_adapter as rb
from hub.config import Secrets
from hub.models import HubError, StateIn

pytestmark = pytest.mark.skipif(not rb.AVAILABLE, reason="pyrainbird needs Python 3.13")

if rb.AVAILABLE:
    from pyrainbird.exceptions import (RainbirdApiException, RainbirdAuthException, RainbirdAuthLockoutException,
                                       RainbirdCertificateError, RainbirdConnectionError, RainbirdDeviceBusyException,
                                       RainbirdDeviceNackError)


def adapter(tmp_path, configured=True):
    a = rb.RainBirdAdapter(Secrets(tmp_path / "s.json"))
    if configured:
        a.secrets.set("rainbird", {"host": "192.168.1.40", "password": "pw", "zone_names": {}})
    return a


def test_every_failure_has_a_plain_explanation():
    host = "192.168.1.40"
    cases = [(RainbirdAuthLockoutException("x"), "lockout", "15 minutes"), (RainbirdAuthException("x"), "auth", "local password"),
             (RainbirdDeviceBusyException("x"), "busy", "Close the Rain Bird app"), (RainbirdCertificateError("x"), "certificate", "certificate"),
             (RainbirdDeviceNackError("x"), "nack", "NACK"), (RainbirdConnectionError("x"), "connection", "router's device list"),
             (asyncio.TimeoutError(), "connection", host), (ConnectionRefusedError(), "connection", host),
             (RainbirdApiException("odd"), "other", "odd")]
    for exc, kind, words in cases:
        assert rb.error_kind(exc) == kind
        assert words in rb.explain(exc, host)
    assert "stopped trying" in rb.explain(RainbirdAuthLockoutException("x"), host)  # it says the hub won't extend the lockout


def test_a_rejected_password_stops_the_polling_instead_of_hammering_the_controller(tmp_path):
    a = adapter(tmp_path)
    calls = []

    async def controller(cfg=None):
        calls.append(1)
        raise RainbirdAuthException("denied")
    a._controller = controller
    assert asyncio.run(a._watch_once(30)) == 900
    assert a._halted() and "local password" in a.watch_error
    for _ in range(5):                       # the loop keeps waking up, but it must not send another password
        asyncio.run(a._watch_once(30))
    assert len(calls) == 1


def test_a_lockout_pauses_for_sixteen_minutes_then_tries_again(tmp_path, monkeypatch):
    a = adapter(tmp_path)
    calls = []

    async def controller(cfg=None):
        calls.append(1)
        raise RainbirdAuthLockoutException("locked")
    a._controller = controller
    now = [1000.0]
    monkeypatch.setattr(rb.time, "monotonic", lambda: now[0])
    assert asyncio.run(a._watch_once(30)) == 16 * 60 and "15 minutes" in a.watch_error
    now[0] += 10 * 60
    asyncio.run(a._watch_once(30))
    assert len(calls) == 1                   # still paused
    now[0] += 7 * 60
    asyncio.run(a._watch_once(30))
    assert len(calls) == 2                   # 17 minutes later: one more attempt


def test_an_unreachable_controller_is_retried_with_a_growing_delay_and_recovers(tmp_path):
    a = adapter(tmp_path)
    state = {"fail": True}

    async def controller(cfg=None):
        if state["fail"]:
            raise RainbirdConnectionError("no route")
        return SimpleNamespace(get_combined_controller_state=lambda: _done(SimpleNamespace(
            irrigation_state=0, active_station=0, remaining_runtime=0, delay_setting=0, sensor_state=0)))

    async def _done(value):
        return value
    a._controller = controller
    delays, delay = [], 30
    for _ in range(6):
        delay = asyncio.run(a._watch_once(delay))
        delays.append(delay)
    assert delays == [60, 120, 240, 300, 300, 300] and not a._halted() and "Can't reach" in a.watch_error
    state["fail"] = False
    assert asyncio.run(a._watch_once(delay)) == rb.WATCH_SECONDS and a.watch_error is None


def test_connect_reports_the_real_reason_and_a_good_password_resumes_polling(tmp_path):
    a = adapter(tmp_path)
    for exc, status, words in ((RainbirdAuthLockoutException("x"), 423, "15 minutes"), (RainbirdAuthException("x"), 401, "local password"),
                               (RainbirdConnectionError("x"), 504, "device list")):
        async def controller(cfg=None, exc=exc):
            raise exc
        a._controller = controller
        with pytest.raises(HubError) as err:
            asyncio.run(a.connect("192.168.1.40", "pw"))
        assert err.value.status == status and words in err.value.message

    a._halt_until, a.watch_error = float("inf"), "paused"

    async def good(cfg=None):
        a._zones, a._model = [1, 2], "ESP-TM2"
    a._controller = good
    assert asyncio.run(a.connect("192.168.1.40", "right"))["zones"] == 2
    assert not a._halted() and a.watch_error is None


def test_controller_requests_are_sent_one_at_a_time(tmp_path):
    a = adapter(tmp_path)
    flight = {"now": 0, "max": 0}

    class Ctl:
        async def _busy(self):
            flight["now"] += 1
            flight["max"] = max(flight["max"], flight["now"])
            await asyncio.sleep(0.01)
            flight["now"] -= 1

        async def irrigate_zone(self, zone, minutes): await self._busy()
        async def stop_irrigation(self): await self._busy()
        async def set_rain_delay(self, days): await self._busy()

        async def get_combined_controller_state(self):
            await self._busy()
            return SimpleNamespace(irrigation_state=0, active_station=0, remaining_runtime=0, delay_setting=0, sensor_state=0)

        async def get_schedule(self):
            await self._busy()
            return SimpleNamespace(programs=[], zone_schedules={})

    a._ctl, a._zones = Ctl(), [1, 2, 3]

    async def go():
        await asyncio.gather(a.set_state("rainbird:controller", StateIn(zone=1, minutes=5)),
                             a.set_state("rainbird:controller", StateIn(rain_delay_days=1)),
                             a.set_state("rainbird:controller", StateIn(action="stop")),
                             a._refresh(), a._watch_once(30))
    asyncio.run(go())
    assert flight["max"] == 1               # without the lock these overlap and the controller answers 503


def test_set_state_turns_library_errors_into_readable_ones(tmp_path):
    a = adapter(tmp_path)

    class Busy:
        async def stop_irrigation(self):
            raise RainbirdDeviceBusyException("busy")
    a._ctl, a._zones = Busy(), [1]
    with pytest.raises(HubError) as err:
        asyncio.run(a.set_state("rainbird:controller", StateIn(action="stop")))
    assert err.value.status == 502 and "Close the Rain Bird app" in err.value.message


def fake_connection(monkeypatch, open_ports):
    async def open_connection(host, port):
        if port not in open_ports:
            raise ConnectionRefusedError()
        return None, SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)


def test_connection_test_walks_through_the_steps(tmp_path, monkeypatch):
    a = adapter(tmp_path, configured=False)
    fake_connection(monkeypatch, open_ports=set())
    [step] = asyncio.run(a.diagnose("192.168.1.99", "pw"))
    assert step["ok"] is False and "Nothing answers" in step["title"] and "device list" in step["detail"]

    fake_connection(monkeypatch, open_ports={443})
    attempts = []

    async def good(session, host, password):
        attempts.append(password)
        return SimpleNamespace(get_model_and_version=lambda: _val(SimpleNamespace(model_name="ESP-ME3")),
                               get_available_stations=lambda: _val(SimpleNamespace(active_set={1, 2, 3})))
    monkeypatch.setattr(rb, "create_controller", good)
    steps = asyncio.run(a.diagnose("192.168.1.40", "pw"))
    assert [s["ok"] for s in steps] == [True, True] and "ESP-ME3, 3 zone(s)" in steps[1]["detail"] and attempts == ["pw"]

    async def denied(session, host, password):
        raise RainbirdAuthException("denied")
    monkeypatch.setattr(rb, "create_controller", denied)
    steps = asyncio.run(a.diagnose("192.168.1.40", "wrong"))
    assert [s["ok"] for s in steps] == [True, False] and "local password" in steps[1]["detail"]


async def _val(value):
    return value


def test_connection_test_never_sends_a_password_while_the_hub_is_paused(tmp_path, monkeypatch):
    a = adapter(tmp_path)
    a._halt_until, a.watch_error = float("inf"), "The controller rejected the password."
    sent = []
    monkeypatch.setattr(rb, "create_controller", lambda *args: sent.append(1))
    [step] = asyncio.run(a.diagnose("192.168.1.40", "pw"))
    assert step["ok"] is False and "paused" in step["title"] and not sent


def test_test_route_uses_typed_values_or_saved_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        assert client.post("/api/rainbird/test", json={}, headers=token).status_code == 400   # nothing typed, nothing saved
        seen = []

        async def diagnose(host, password):
            seen.append((host, password))
            return [{"ok": True, "title": "t", "detail": "d"}]
        monkeypatch.setattr(hub.rainbird, "diagnose", diagnose)
        assert client.post("/api/rainbird/test", json={"host": "10.0.0.5", "password": "abc"}, headers=token).json()["steps"][0]["ok"]
        hub.secrets.set("rainbird", {"host": "10.0.0.9", "password": "saved"})
        client.post("/api/rainbird/test", json={}, headers=token)
        assert seen == [("10.0.0.5", "abc"), ("10.0.0.9", "saved")]
        assert client.post("/api/rainbird/test", json={"host": "not-an-ip"}, headers=token).status_code == 422
