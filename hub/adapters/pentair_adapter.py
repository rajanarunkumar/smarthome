"""Pentair Home (cloud) pool equipment (pypentair, the library the Home
Assistant Pentair Home integration uses).

Readings for every device. IntelliFlo VSF pumps (type IF31) can also be turned
on and off: the hub sends {"d25": "1"/"0"} with a PUT to the device, which is
what the open-source homebridge-pentair-cloud plugin does. Pentair doesn't
publish this API. Schedules for the pump run in the hub (see scheduler.py);
the pump's own programs and schedules from the Pentair app aren't documented
anywhere, so the hub doesn't change them.

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
POWER_COMMAND = {"IF31": "d25"}               # IntelliFlo pump: run "1" / stop "0"
DEVICE_PATH = "device/device-service/user/device/{device_id}"

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
        self._at = float("-inf")  # never

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
        self._at = float("-inf")  # never
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
                details=details, capabilities=["onoff"] if d.get("deviceType") in POWER_COMMAND else [],
            ))
        return out

    def names(self) -> dict[str, str]:
        return {f"pentair:{i}": (d.get("productInfo") or {}).get("nickName") or "Pentair device"
                for i, d in self._devices.items()}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        dev_id = device_id.split(":", 1)[1]
        device = self._devices.get(dev_id) or {}
        key = POWER_COMMAND.get(device.get("deviceType", ""))
        if key is None or state.on is None:
            raise HubError(400, "The hub can only turn Pentair IntelliFlo pumps on and off. Use the Pentair Home app for the rest.")

        def send() -> None:
            client = self._client()
            # pypentair has no public command call; this reuses its signed request helper.
            client._Pentair__request("put", DEVICE_PATH, path_params={"device_id": dev_id},
                                     json={"payload": {key: "1" if state.on else "0"}})

        try:
            await asyncio.to_thread(send)
        except PentairAuthenticationError as exc:
            raise HubError(401, "Pentair sign-in expired. Connect again in Settings.") from exc
        except Exception as exc:  # noqa: BLE001
            raise HubError(502, f"Pentair didn't accept the command: {exc}") from exc
        self._at = float("-inf")  # never

    def raw_fields(self) -> list[dict]:
        """Every field of every device, secrets removed: for working out the pump's schedule fields."""
        return [{"name": (d.get("productInfo") or {}).get("nickName"), "deviceType": d.get("deviceType"),
                 "fields": {k: v for k, v in (d.get("fields") or {}).items() if k not in SECRET_FIELDS}}
                for d in self._devices.values()]

    def readings(self) -> list[dict]:
        """Live flow, power, speed and schedule of each IntelliFlo pump, from the last fetch."""
        from ..pool import parse_programs, parse_reading
        out = []
        for dev_id, d in self._devices.items():
            fields = d.get("fields") or {}
            if d.get("deviceType") in RUNNING_FIELD and fields:
                out.append({"id": f"pentair:{dev_id}", "name": (d.get("productInfo") or {}).get("nickName") or "Pool pump",
                            "reading": parse_reading(fields), "programs": parse_programs(fields)})
        return out

    async def status(self) -> dict:
        return {"connected": self.active, "available": AVAILABLE, "devices": len(self._devices)}
