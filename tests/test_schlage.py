"""Schlage locks: the adapter, and the hub's safety rules for unlocking."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hub.adapters.schlage_adapter import SchlageAdapter, describe
from hub.config import Secrets
from hub.models import HubError, StateIn


class FakeLock:
    def __init__(self, device_id="L1", locked=True):
        self.device_id, self.name, self.model_name = device_id, "Front door", "BE489WB"
        self.connected, self.is_locked, self.is_jammed = True, locked, False
        self.battery_level, self.door_state = 82, SimpleNamespace(value="CLOSED")
        self.calls = []

    def last_changed_by(self):
        return "keypad - Alex"

    def lock(self):
        self.calls.append("lock"); self.is_locked = True

    def unlock(self):
        self.calls.append("unlock"); self.is_locked = False


def test_describe():
    lock = FakeLock()
    assert describe(lock) == "Locked by keypad: Alex · Door closed"
    lock.is_jammed = True
    assert describe(lock) == "Jammed: check the door"
    lock.connected = False
    assert describe(lock) == "Offline"


def adapter_with(tmp_path, lock):
    adapter = SchlageAdapter(Secrets(tmp_path / "s.json"))
    adapter.secrets.set("schlage", {"email": "hub@example.com", "password": "pw"})
    adapter._api = SimpleNamespace(locks=lambda: [lock])
    return adapter


def test_lock_unlock_and_plain_off_is_refused(tmp_path):
    lock = FakeLock()
    adapter = adapter_with(tmp_path, lock)
    [d] = asyncio.run(adapter.devices())
    assert (d.kind, d.on, d.battery, d.capabilities) == ("lock", True, 82, ["lock"])
    asyncio.run(adapter.set_state(d.id, StateIn(locked=False)))
    asyncio.run(adapter.set_state(d.id, StateIn(locked=True)))
    with pytest.raises(HubError):  # an "off" must never unlock a door
        asyncio.run(adapter.set_state(d.id, StateIn(on=False)))
    assert lock.calls == ["unlock", "lock"]


@pytest.fixture
def hub_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        lock = FakeLock(locked=True)
        hub.secrets.set("schlage", {"email": "hub@example.com", "password": "pw"})
        hub.schlage._api = SimpleNamespace(locks=lambda: [lock])
        yield client, hub, lock


def test_api_token_can_lock_but_not_unlock(hub_client):
    client, hub, lock = hub_client
    token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
    client.get("/api/devices", headers=token)
    r = client.post("/api/devices/schlage:L1/state", json={"locked": False}, headers=token)
    assert r.status_code == 403 and lock.calls == []
    r = client.post("/api/devices/schlage:L1/state", json={"locked": True}, headers=token)
    assert r.status_code == 200 and lock.calls == ["lock"]


def test_signed_in_person_can_unlock(hub_client, monkeypatch):
    client, hub, lock = hub_client
    client.get("/api/devices", headers={"Authorization": f"Bearer {hub.secrets.access_token()}"})
    hub.secrets.set("auth", {"origins": ["https://hub.example.ts.net"]})
    monkeypatch.setattr(hub.auth, "session", lambda cookie: {"email": "me@example.com", "method": "passkey"})
    origin = {"Origin": "https://hub.example.ts.net"}
    r = client.post("/api/devices/schlage:L1/state", json={"locked": False}, headers=origin)
    assert r.status_code == 200 and lock.calls == ["unlock"]


def test_room_all_off_never_unlocks(hub_client):
    client, hub, lock = hub_client
    token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
    client.get("/api/devices", headers=token)
    room = hub.store.add_room("Entry")
    hub.store.set_device_room("schlage:L1", room["id"])
    r = client.post(f"/api/rooms/{room['id']}/state", json={"on": False}, headers=token)
    assert r.status_code == 200 and lock.calls == []
