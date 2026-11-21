"""Pentair Home (cloud) pool equipment, read-only (pypentair, the library the
Home Assistant Pentair Home integration uses).

Pentair has no public API for controlling Pentair Home devices, and no public
source documents how the app sends commands, so the hub shows each device's
readings (pump running, speed, power, temperatures…) but doesn't change them.
Sign in with the Pentair Home email and password in Settings; the hub keeps
only the login tokens, never the password.
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
REFRESH_SECONDS = 120
SECRET_FIELDS = {"s11", "d15", "s6", "s23"}  # security key, password, Wi-Fi MAC, Wi-Fi name
RUNNING_FIELD = {"IF31": "s25"}               # IntelliFlo pump: "pump enabled"

try:
    from pypentair import Pentair
    from pypentair.exceptions import PentairAuthenticationError
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

    class PentairAuthenticationError(Exception):
        pass


def unwrap(response: Any) -> Any:
    return response.get("data", response) if isinstance(response, dict) else response


def field_value(field: Any) -> Any:
    return field.get("value", field) if isinstance(field, dict) else field


def details_of(device: dict) -> list[dict[str, str]]:
    """Readable fields: [{"label", "value"}], skipping secrets and diagnostics."""
    out = []
    for key, field in (device.get("fields") or {}).items():
        if key in SECRET_FIELDS or not isinstance(field, dict) or field.get("category", "data") != "data":
            continue
        value = field_value(field)
        if value in (None, ""):
            continue
        label = field.get("name") or key
        unit = field.get("unit") or ""
        if key == RUNNING_FIELD.get(device.get("deviceType", "")):
            value = "Yes" if str(value).lower() in ("1", "true", "on") else "No"
        out.append({"label": str(label), "value": f"{value} {unit}".strip()})
    return out


def is_running(device: dict) -> bool | None:
    key = RUNNING_FIELD.get(device.get("deviceType", ""))
    if not key or key not in (device.get("fields") or {}):
        return None
    return str(field_value(device["fields"][key])).lower() in ("1", "true", "on")


class PentairAdapter(Adapter):
    brand = "pentair"
    label = "Pentair"
    prefixes = ("pentair",)
    can_rename = False

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._devices: dict[str, dict] = {}
        self._at = 0.0

    @property
    def config(self) -> dict:
        return self.secrets.get("pentair") or {}

    @property
    def active(self) -> bool:
        return AVAILABLE and bool(self.config.get("refresh_token"))

    def _client(self) -> Any:
        cfg = self.config
        return Pentair(username=cfg["email"], access_token=cfg.get("access_token"),
                       id_token=cfg.get("id_token"), refresh_token=cfg["refresh_token"])

    def _fetch(self) -> list[dict]:
        """Blocking: list devices, then read each one's fields. Saves renewed tokens."""
        client = self._client()
        listed = unwrap(client.get_devices()) or []
        full = []
        for d in listed:
            try:
                full.append(unwrap(client.get_device(d["deviceId"])) or d)
            except Exception as exc:  # noqa: BLE001
                log.warning("Pentair device %s failed: %s", d.get("deviceId"), exc)
                full.append(d)
        tokens = client.get_tokens()
        if tokens and tokens.get("refresh_token"):
            self.secrets.set("pentair", {**self.config, **tokens})
        return full

    async def connect(self, email: str, password: str) -> dict:
        if not AVAILABLE:
            raise HubError(501, "Pentair support isn't installed. Run pip install -r requirements.txt.")

        def login() -> dict:
            client = Pentair(username=email.strip())
            client.authenticate(password)
            return client.get_tokens()

        try:
            tokens = await asyncio.to_thread(login)
        except PentairAuthenticationError as exc:
            raise HubError(401, "Pentair didn't accept that email and password.") from exc
        self.secrets.set("pentair", {"email": email.strip(), **tokens})
        self._at = 0.0
        found = await self.devices()
        return {"connected": True, "devices": len(found)}

    async def disconnect(self) -> None:
        self.secrets.set("pentair", None)
        self._devices = {}

    async def devices(self) -> list[Device]:
        if time.monotonic() - self._at > REFRESH_SECONDS:
            try:
                fetched = await asyncio.to_thread(self._fetch)
                self._devices = {d["deviceId"]: d for d in fetched if d.get("deviceId")}
                self._at = time.monotonic()
            except PentairAuthenticationError as exc:
                raise HubError(401, "Pentair sign-in expired. Connect again in Settings.") from exc
        out = []
        for dev_id, d in self._devices.items():
            info = d.get("productInfo") or {}
            details = details_of(d)
            running = is_running(d)
            out.append(Device(
                id=f"pentair:{dev_id}", brand=self.brand, kind="pool",
                name=info.get("nickName") or d.get("pname") or "Pentair device",
                model=" ".join(x for x in (info.get("maker"), info.get("model")) if x) or d.get("deviceType"),
                online=bool(d.get("fields")), on=running,
                media=("Running" if running else "Off") if running is not None else
                      (" · ".join(f"{x['label']} {x['value']}" for x in details[:2]) or None),
                details=details, capabilities=[],
            ))
        return out

    def names(self) -> dict[str, str]:
        return {f"pentair:{i}": (d.get("productInfo") or {}).get("nickName") or "Pentair device"
                for i, d in self._devices.items()}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        raise HubError(400, "Pentair devices are view-only in the hub. Use the Pentair Home app to change them.")

    async def status(self) -> dict:
        return {"connected": self.active, "available": AVAILABLE, "devices": len(self._devices)}
