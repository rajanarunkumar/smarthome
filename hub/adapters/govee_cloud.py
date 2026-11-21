"""Govee developer API (cloud). Needs a free API key from the Govee app:
Profile → Settings (gear) → Apply for API Key; the key arrives by email.

Used for Govee devices that the LAN API can't reach (no "LAN Control"), for
plugs, and for the device names from the Govee app. Govee limits state reads
to 30 a minute per device, so state is cached for REFRESH_SECONDS.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from ..models import HubError

BASE_URL = "https://openapi.api.govee.com/router/api/v1"
REFRESH_SECONDS = 120
DEVICE_LIST_SECONDS = 3600

ON_OFF = ("devices.capabilities.on_off", "powerSwitch")
BRIGHTNESS = ("devices.capabilities.range", "brightness")
RGB = ("devices.capabilities.color_setting", "colorRgb")
KELVIN = ("devices.capabilities.color_setting", "colorTemperatureK")
ONLINE = ("devices.capabilities.online", "online")
KINDS = {"devices.types.light": "light", "devices.types.socket": "plug"}


def hub_id(device: str) -> str:
    """Same id the LAN API produces, so one light never shows up twice."""
    return "govee:" + device.replace(":", "").lower()


def caps_of(device: dict) -> set[tuple[str, str]]:
    return {(c.get("type"), c.get("instance")) for c in device.get("capabilities", [])}


def kelvin_range(device: dict) -> tuple[int, int] | None:
    for c in device.get("capabilities", []):
        if (c.get("type"), c.get("instance")) == KELVIN:
            r = (c.get("parameters") or {}).get("range") or {}
            if r.get("min") and r.get("max"):
                return int(r["min"]), int(r["max"])
    return None


def rgb_to_int(hex_color: str) -> int:
    return int(hex_color.lstrip("#"), 16)


def int_to_rgb(value: int) -> str:
    return f"#{int(value) & 0xFFFFFF:06x}"


def parse_state(capabilities: list[dict]) -> dict:
    """Turn a /device/state reply into hub Device fields."""
    values = {(c.get("type"), c.get("instance")): (c.get("state") or {}).get("value") for c in capabilities}
    out: dict[str, Any] = {}
    if values.get(ONLINE) is not None:
        out["online"] = bool(values[ONLINE])
    if values.get(ON_OFF) is not None:
        out["on"] = bool(values[ON_OFF])
    if values.get(BRIGHTNESS):
        out["brightness"] = int(values[BRIGHTNESS])
    if values.get(KELVIN):
        out["mirek"] = round(1_000_000 / int(values[KELVIN]))
    elif values.get(RGB):
        out["color"] = int_to_rgb(values[RGB])
    return out


class GoveeCloud:
    def __init__(self, api_key: str) -> None:
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=10,
                                       headers={"Govee-API-Key": api_key, "Content-Type": "application/json"})
        self.devices: dict[str, dict] = {}     # hub id -> device record from /user/devices
        self.state: dict[str, dict] = {}       # hub id -> parsed state
        self._listed = 0.0
        self._refreshed: dict[str, float] = {}

    async def _call(self, method: str, path: str, payload: dict | None = None) -> Any:
        body = None if payload is None else {"requestId": str(uuid.uuid4()), "payload": payload}
        try:
            r = await self._http.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise HubError(504, f"Couldn't reach Govee: {exc}") from exc
        if r.status_code == 401:
            raise HubError(401, "Govee rejected the API key. Check it in Settings.")
        if r.status_code == 429:
            raise HubError(429, "Govee is rate-limiting the hub. Try again in a minute.")
        data = r.json() if r.content else {}
        if r.status_code >= 400 or data.get("code") not in (None, 200):
            raise HubError(502, f"Govee said: {data.get('msg') or data.get('message') or r.status_code}")
        return data

    async def list_devices(self, force: bool = False) -> dict[str, dict]:
        if force or not self.devices or time.monotonic() - self._listed > DEVICE_LIST_SECONDS:
            data = await self._call("GET", "/user/devices")
            self.devices = {hub_id(d["device"]): d for d in data.get("data", [])
                            if d.get("type") in KINDS}
            self._listed = time.monotonic()
        return self.devices

    async def refresh(self, hid: str) -> dict:
        if time.monotonic() - self._refreshed.get(hid, float("-inf")) < REFRESH_SECONDS:
            return self.state.get(hid, {})
        d = self.devices[hid]
        data = await self._call("POST", "/device/state", {"sku": d["sku"], "device": d["device"]})
        self.state[hid] = parse_state(((data.get("payload") or {}).get("capabilities")) or [])
        self._refreshed[hid] = time.monotonic()
        return self.state[hid]

    async def control(self, hid: str, cap: tuple[str, str], value: Any) -> None:
        d = self.devices[hid]
        await self._call("POST", "/device/control", {
            "sku": d["sku"], "device": d["device"],
            "capability": {"type": cap[0], "instance": cap[1], "value": value},
        })

    async def close(self) -> None:
        await self._http.aclose()
