"""Dyson purifiers and fans, controlled locally over MQTT (libdyson-neon).

Two ways to set up, once. Either way the hub then talks to the devices
directly on the Wi-Fi, and the Dyson login is never stored.

1. Email: Settings → Dyson → the MyDyson email, the code Dyson emails you,
   and the account password. Needs a Dyson password.
2. Sticker: for accounts made with "Sign in with Google" or Apple, which have
   no password. Enter the Wi-Fi name (DYSON-…) and Wi-Fi password printed on
   the sticker on the device or in its manual. libdyson derives the local key
   from those; no Dyson account is involved.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from ..config import Secrets
from ..models import Device, HubError, StateIn
from .base import Adapter

log = logging.getLogger(__name__)

try:
    from libdyson import get_device, get_mqtt_info_from_wifi_info
    from libdyson.cloud import DysonAccount
    from libdyson.discovery import DysonDiscovery
    from libdyson.const import DEVICE_TYPE_NAMES
    from libdyson.exceptions import DysonException
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

    DEVICE_TYPE_NAMES: dict = {}

    class DysonException(Exception):
        pass

OTP_SECONDS = 600
TOGGLES = {  # action id -> (label, property on the libdyson device, turn on, turn off)
    "auto": ("Auto", "auto_mode", "enable_auto_mode", "disable_auto_mode"),
    "oscillation": ("Oscillation", "oscillation", "enable_oscillation", "disable_oscillation"),
    "night": ("Night mode", "night_mode", "enable_night_mode", "disable_night_mode"),
}


def kelvin_to_f(kelvin: float | None) -> int | None:
    return None if kelvin in (None, -1) else round((kelvin - 273.15) * 9 / 5 + 32)


def readings(dev: Any) -> list[dict[str, str]]:
    """Environment readings that this model reports."""
    out = []

    def add(label: str, attr: str, fmt: Callable[[Any], str]) -> None:
        value = getattr(dev, attr, None)
        if value not in (None, -1, "OFF", "INIT"):
            out.append({"label": label, "value": fmt(value)})

    add("Temperature", "temperature", lambda v: f"{kelvin_to_f(v)}°F")
    add("Humidity", "humidity", lambda v: f"{v}%")
    add("PM2.5", "particulate_matter_2_5", lambda v: f"{v} µg/m³")
    add("PM10", "particulate_matter_10", lambda v: f"{v} µg/m³")
    add("VOC", "volatile_organic_compounds", lambda v: f"{v}")
    add("NO₂", "nitrogen_dioxide", lambda v: f"{v}")
    add("HEPA filter", "hepa_filter_life", lambda v: f"{v}% left")
    add("Carbon filter", "carbon_filter_life", lambda v: f"{v}% left")
    return out


def summary(dev: Any) -> str:
    if not dev.is_on:
        return "Off"
    bits = ["Auto" if dev.auto_mode else f"Speed {dev.speed}"]
    pm = getattr(dev, "particulate_matter_2_5", None)
    if pm not in (None, -1):
        bits.append(f"PM2.5 {pm}")
    temp = kelvin_to_f(getattr(dev, "temperature", None))
    if temp is not None:
        bits.append(f"{temp}°F")
    return " · ".join(bits)


class DysonAdapter(Adapter):
    brand = "dyson"
    label = "Dyson"
    prefixes = ("dyson",)
    can_rename = False

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._devs: dict[str, Any] = {}           # hub id -> connected libdyson device
        self._pending_otp: tuple[Callable, float] | None = None
        self._discovery: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def saved(self) -> list[dict]:
        return (self.secrets.get("dyson") or {}).get("devices") or []

    @property
    def active(self) -> bool:
        return AVAILABLE and bool(self.saved)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.active:
            self._discover()

    def _discover(self) -> None:
        """Find saved devices on the LAN (mDNS) and connect to each one as it appears."""
        if self._discovery is not None:
            self._discovery.stop_discovery()
        self._discovery = DysonDiscovery()
        self._discovery.start_discovery()
        for saved in self.saved:
            dev = get_device(saved["serial"], saved["credential"], saved["device_type"])
            if dev is None or not hasattr(dev, "set_speed"):
                continue  # robot vacuums and lights aren't supported by the hub yet

            def on_found(address: str, dev: Any = dev) -> None:
                try:
                    dev.connect(address)
                    self._devs["dyson:" + dev.serial.lower()] = dev
                except DysonException as exc:
                    log.warning("Dyson %s at %s did not connect: %s", dev.serial, address, exc)

            self._discovery.register_device(dev, on_found)

    # Sign-in: step 1 sends a code by email, step 2 checks it.
    async def send_code(self, email: str, country: str) -> dict:
        if not AVAILABLE:
            raise HubError(501, "Dyson support isn't installed. Run pip install -r requirements.txt.")
        account = DysonAccount()
        try:
            verify = await asyncio.to_thread(account.login_email_otp, email.strip(), country.upper())
        except DysonException as exc:
            raise HubError(400, f"Dyson didn't send a code: {type(exc).__name__}. "
                                "Check the email address, or wait a minute and try again.") from exc
        self._pending_otp = (lambda code, pw: (verify(code, pw), account)[1], time.monotonic())
        return {"sent": True}

    async def verify_code(self, code: str, password: str) -> dict:
        if self._pending_otp is None or time.monotonic() - self._pending_otp[1] > OTP_SECONDS:
            raise HubError(409, "Ask for a new code first.")
        try:
            account = await asyncio.to_thread(self._pending_otp[0], code.strip(), password)
            infos = await asyncio.to_thread(account.devices)
        except DysonException as exc:
            raise HubError(401, "Dyson didn't accept the code or password.") from exc
        finally:
            self._pending_otp = None
        saved = [{"serial": i.serial, "credential": i.credential, "device_type": i.get_device_type(),
                  "name": i.name} for i in infos if i.get_device_type()]
        self.secrets.set("dyson", {"devices": saved})
        self._discover()
        return {"connected": True, "devices": len(saved)}

    async def disconnect(self) -> None:
        await self.close()
        self.secrets.set("dyson", None)

    async def add_from_sticker(self, ssid: str, wifi_password: str, name: str = "") -> dict:
        """Add one device from the Wi-Fi name and password on its sticker (no Dyson account)."""
        if not AVAILABLE:
            raise HubError(501, "Dyson support isn't installed. Run pip install -r requirements.txt.")
        try:
            serial, credential, device_type = get_mqtt_info_from_wifi_info(ssid.strip(), wifi_password.strip())
        except DysonException as exc:
            raise HubError(400, "That doesn't look like the Wi-Fi name from a Dyson sticker. It starts with "
                                "DYSON- and looks like DYSON-AB1-US-ABC1234A-438. Copy it exactly.") from exc
        if get_device(serial, credential, device_type) is None:
            raise HubError(400, f"The hub doesn't support Dyson product type {device_type} yet.")
        entry = {"serial": serial, "credential": credential, "device_type": device_type,
                 "name": name.strip()[:32] or DEVICE_TYPE_NAMES.get(device_type, "Dyson").split(" (")[0]}
        saved = [s for s in self.saved if s["serial"].lower() != serial.lower()] + [entry]
        self.secrets.set("dyson", {"devices": saved})
        self._discover()
        return {"connected": True, "devices": len(saved), "name": entry["name"]}

    def _name(self, serial: str) -> str:
        return next((s["name"] for s in self.saved if s["serial"].lower() == serial.lower()), "Dyson")

    async def devices(self) -> list[Device]:
        out = []
        for saved in self.saved:
            hid = "dyson:" + saved["serial"].lower()
            dev = self._devs.get(hid)
            base = dict(id=hid, brand=self.brand, kind="purifier", name=saved["name"], model=saved["device_type"])
            if dev is None or not dev.is_connected:
                out.append(Device(**base, online=False, media="Looking for it on the network…"))
                continue
            out.append(Device(
                **base, on=dev.is_on, media=summary(dev), level=None if dev.auto_mode else dev.speed, level_max=10,
                details=readings(dev),
                actions=[{"id": k, "label": f"{label}: {'on' if getattr(dev, prop) else 'off'}"}
                         for k, (label, prop, *_rest) in TOGGLES.items() if getattr(dev, prop, None) is not None],
                capabilities=["onoff", "level", "actions"],
            ))
        return out

    def names(self) -> dict[str, str]:
        return {"dyson:" + s["serial"].lower(): s["name"] for s in self.saved}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        dev = self._devs.get(device_id)
        if dev is None or not dev.is_connected:
            raise HubError(503, "The Dyson isn't connected yet. Check that it's powered and on Wi-Fi.")

        def apply() -> None:
            if state.on is True:
                dev.turn_on()
            elif state.on is False:
                dev.turn_off()
            if state.level is not None:
                dev.set_speed(state.level)  # also leaves auto mode
            if state.action:
                if state.action not in TOGGLES:
                    raise HubError(400, "Unknown Dyson command")
                _, prop, turn_on, turn_off = TOGGLES[state.action]
                getattr(dev, turn_off if getattr(dev, prop) else turn_on)()

        await asyncio.to_thread(apply)

    async def status(self) -> dict:
        return {"connected": bool(self.saved), "available": AVAILABLE, "devices": len(self.saved),
                "online": sum(1 for d in self._devs.values() if d.is_connected),
                "waiting_for_code": self._pending_otp is not None}

    async def close(self) -> None:
        for dev in self._devs.values():
            try:
                await asyncio.to_thread(dev.disconnect)
            except Exception:  # noqa: BLE001
                pass
        self._devs.clear()
        if self._discovery is not None:
            self._discovery.stop_discovery()
            self._discovery = None
