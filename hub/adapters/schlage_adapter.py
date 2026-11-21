"""Schlage Wi-Fi locks (Encode, Encode Plus, Encode Lever) through the Schlage
Home cloud, using pyschlage, the library Home Assistant's Schlage integration uses.

Schlage's sign-in needs the account password every time a session is renewed,
so the hub keeps it in ~/.smarthome/secrets.json (readable only by your Mac
user). Best practice: in the Schlage Home app, share the lock with a second
Schlage account and give the hub that one, so it can be revoked on its own.

Safety rules (enforced here and in app.py):
- Locks have no on/off switch, and room "All on / All off" never touches them.
- Unlocking needs a signed-in person (Google or Face ID); API-token scripts can lock only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import Secrets
from ..models import Device, HubError, StateIn
from .base import Adapter

log = logging.getLogger(__name__)
REFRESH_SECONDS = 30

try:
    from pyschlage import Auth, Schlage
    from pyschlage.exceptions import Error as SchlageError
    from pyschlage.exceptions import NotAuthorizedError
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

    class SchlageError(Exception):
        pass

    class NotAuthorizedError(SchlageError):
        pass


def describe(lock: Any) -> str:
    """'Locked · by keypad - Alex · Door closed', 'Jammed', 'Offline'."""
    if not lock.connected:
        return "Offline"
    if lock.is_jammed:
        return "Jammed: check the door"
    bits = ["Locked" if lock.is_locked else "Unlocked"]
    try:
        who = lock.last_changed_by()
    except Exception:  # noqa: BLE001 - who-did-it is nice to have, never required
        who = None
    if who and who != "unknown":
        bits[0] += f" by {who.replace(' - ', ': ')}"
    door = getattr(lock, "door_state", None)
    if door is not None:
        bits.append(f"Door {str(getattr(door, 'value', door)).lower()}")
    return " · ".join(bits)


class SchlageAdapter(Adapter):
    brand = "schlage"
    label = "Schlage"
    prefixes = ("schlage",)
    can_rename = False

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._api: Any = None
        self._locks: dict[str, Any] = {}
        self._at = 0.0
        self._lock = asyncio.Lock()

    @property
    def config(self) -> dict:
        return self.secrets.get("schlage") or {}

    @property
    def active(self) -> bool:
        return AVAILABLE and bool(self.config.get("password"))

    def _client(self) -> Any:
        if self._api is None:
            self._api = Schlage(Auth(self.config["email"], self.config["password"]))
        return self._api

    async def connect(self, email: str, password: str) -> dict:
        if not AVAILABLE:
            raise HubError(501, "Schlage support isn't installed. Run pip install -r requirements.txt.")

        def login() -> Any:
            auth = Auth(email.strip(), password)
            auth.authenticate()
            api = Schlage(auth)
            return api, api.locks()

        try:
            api, locks = await asyncio.to_thread(login)
        except NotAuthorizedError as exc:
            raise HubError(401, "Schlage didn't accept that email and password.") from exc
        except SchlageError as exc:
            raise HubError(502, f"Couldn't reach Schlage: {exc}") from exc
        self.secrets.set("schlage", {"email": email.strip(), "password": password})
        self._api = api
        self._locks = {"schlage:" + lk.device_id: lk for lk in locks}
        self._at = time.monotonic()
        return {"connected": True, "devices": len(locks)}

    async def disconnect(self) -> None:
        self.secrets.set("schlage", None)
        self._api = None
        self._locks = {}

    async def _refresh(self) -> None:
        async with self._lock:
            if time.monotonic() - self._at < REFRESH_SECONDS and self._locks:
                return
            try:
                locks = await asyncio.to_thread(self._client().locks)
            except NotAuthorizedError as exc:
                self._api = None
                raise HubError(401, "Schlage sign-in failed. Connect again in Settings.") from exc
            self._locks = {"schlage:" + lk.device_id: lk for lk in locks}
            self._at = time.monotonic()

    async def devices(self) -> list[Device]:
        await self._refresh()
        return [Device(
            id=hid, brand=self.brand, kind="lock", name=lk.name or "Lock", model=lk.model_name,
            online=bool(lk.connected), on=bool(lk.is_locked), media=describe(lk), battery=lk.battery_level,
            capabilities=["lock"],
        ) for hid, lk in self._locks.items()]

    def names(self) -> dict[str, str]:
        return {h: lk.name or "Lock" for h, lk in self._locks.items()}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        lk = self._locks.get(device_id)
        if lk is None:
            raise HubError(404, f"Unknown Schlage lock {device_id}")
        if state.locked is None:
            # A plain on/off (e.g. a room's "All off") must never unlock a door.
            raise HubError(400, "Locks only take lock or unlock.")
        try:
            await asyncio.to_thread(lk.lock if state.locked else lk.unlock)
        except SchlageError as exc:
            raise HubError(502, f"The lock didn't respond: {exc}") from exc
        self._at = 0.0  # show the new state on the next refresh

    async def status(self) -> dict:
        return {"connected": self.active, "available": AVAILABLE, "devices": len(self._locks)}
