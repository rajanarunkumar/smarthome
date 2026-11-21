"""LG ThinQ appliances over LG's ThinQ Connect API (cloud).

Needs a free Personal Access Token: sign in at https://connect-pat.lgthinq.com
with your LG account, choose "Add new token", tick every permission, and paste
the token into the hub's Settings → LG ThinQ. It is saved only on this Mac.

Climate devices (AC, air purifier, dehumidifier, humidifier, fans) can be
turned on and off. Washers and dryers can be started, paused, powered off and
given a delayed start, following LG's own safety rule: a cycle can only be
started after someone presses Remote Start on the machine. LG's API has no way
to choose the wash course; set it on the machine before pressing Remote Start.
Dishwashers, ovens and cooktops stay read-only.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from typing import Any

import httpx

from ..config import Secrets
from ..models import Device, HubError, StateIn
from .base import Adapter

log = logging.getLogger(__name__)
STATUS_SECONDS = 60
LIST_SECONDS = 3600

# LG's public client key for ThinQ Connect, published in LG's open-source SDK (thinqconnect).
API_KEY = "v6GFvkweNo7DK7yD3ylIZ9w52aKBU0eJ7wLXkSR3"

# Which LG server a country's accounts live on (from LG's SDK). Everything else is "eic".
_AIC = set("AG AR AW BB BO BR BS BZ CA CL CO CR CU DM DO EC GD GT GY HN HT JM KN LC MX NI PA PE PR PY SR SV "
           "TT US UY VC VE".split())
_KIC = set("AU BD CN HK ID IN JP KH KR LA LK MM MY NP NZ PH SG TH TW VN".split())

# Device type → (label, power can be switched by the hub)
TYPES = {
    "DEVICE_AIR_CONDITIONER": ("Air conditioner", True),
    "DEVICE_AIR_PURIFIER": ("Air purifier", True),
    "DEVICE_AIR_PURIFIER_FAN": ("Air purifier fan", True),
    "DEVICE_DEHUMIDIFIER": ("Dehumidifier", True),
    "DEVICE_HUMIDIFIER": ("Humidifier", True),
    "DEVICE_CEILING_FAN": ("Ceiling fan", True),
    "DEVICE_WASHER": ("Washer", False),
    "DEVICE_DRYER": ("Dryer", False),
    "DEVICE_WASHTOWER": ("WashTower", False),
    "DEVICE_WASHTOWER_WASHER": ("Washer", False),
    "DEVICE_WASHTOWER_DRYER": ("Dryer", False),
    "DEVICE_WASHCOMBO_MAIN": ("Washer-dryer", False),
    "DEVICE_WASHCOMBO_MINI": ("Mini washer", False),
    "DEVICE_STYLER": ("Styler", False),
    "DEVICE_DISH_WASHER": ("Dishwasher", False),
    "DEVICE_REFRIGERATOR": ("Refrigerator", False),
    "DEVICE_KIMCHI_REFRIGERATOR": ("Kimchi refrigerator", False),
    "DEVICE_WINE_CELLAR": ("Wine cellar", False),
    "DEVICE_OVEN": ("Oven", False),
    "DEVICE_MICROWAVE_OVEN": ("Microwave", False),
    "DEVICE_COOKTOP": ("Cooktop", False),
    "DEVICE_HOOD": ("Range hood", False),
    "DEVICE_ROBOT_CLEANER": ("Robot vacuum", False),
    "DEVICE_WATER_HEATER": ("Water heater", False),
    "DEVICE_WATER_PURIFIER": ("Water purifier", False),
}


LAUNDRY = {"DEVICE_WASHER", "DEVICE_DRYER", "DEVICE_WASHTOWER_WASHER", "DEVICE_WASHTOWER_DRYER",
           "DEVICE_WASHCOMBO_MAIN", "DEVICE_WASHCOMBO_MINI", "DEVICE_STYLER"}
ACTION_LABELS = {"START": "Start", "STOP": "Pause", "POWER_OFF": "Power off", "WAKE_UP": "Wake up"}
NEEDS_REMOTE_START = {"START"}


def region_of(country: str) -> str:
    country = country.upper()
    return "aic" if country in _AIC else "kic" if country in _KIC else "eic"


def _words(value: str) -> str:
    return value.replace("_", " ").capitalize()


def _parts(status: Any) -> list[dict]:
    """Status is one dict, or a list of dicts for multi-part devices (WashTower, fridge zones)."""
    if isinstance(status, list):
        return [s for s in status if isinstance(s, dict)]
    return [status] if isinstance(status, dict) else []


def power_key(status: Any) -> tuple[str, bool] | None:
    """(operation key, is on) when the device reports a POWER_ON/POWER_OFF mode."""
    for part in _parts(status):
        for key, value in (part.get("operation") or {}).items():
            if key.endswith("OperationMode") and value in ("POWER_ON", "POWER_OFF"):
                return key, value == "POWER_ON"
    return None


def laundry_controls(profile: Any) -> dict | None:
    """From a device profile: the operation key, the modes LG accepts, and the delay range.

    Multi-part machines (a WashTower reported as one device) return None: the
    hub shows their status but can't tell which half a command is for.
    """
    props = (profile or {}).get("property")
    if not isinstance(props, dict):
        return None
    out: dict[str, Any] = {"key": None, "modes": [], "delay": None}
    for key, spec in (props.get("operation") or {}).items():
        if key.endswith("OperationMode") and isinstance(spec, dict) and "w" in spec.get("mode", []):
            values = (spec.get("value") or {}).get("w") or []
            out["key"] = key
            out["modes"] = [v for v in ACTION_LABELS if v in values]
    delay = (props.get("timer") or {}).get("relativeHourToStart")
    if isinstance(delay, dict) and "w" in delay.get("mode", []):
        rng = (delay.get("value") or {}).get("w") or {}
        if rng.get("max"):
            out["delay"] = (int(rng.get("min") or 1), int(rng["max"]))
    return out if out["key"] else None


def remote_start_on(status: Any) -> bool:
    return any((p.get("remoteControlEnable") or {}).get("remoteControlEnabled") for p in _parts(status))


def summarize(status: Any) -> str | None:
    """A short human status like 'Running · 32 min left' or 'Cool · 72° → 70°'."""
    bits: list[str] = []
    for part in _parts(status):
        run = (part.get("runState") or {}).get("currentState")
        if run and run not in ("POWER_OFF", "SLEEP"):
            bits.append(_words(run))
        timer = part.get("timer") or {}
        mins = (timer.get("remainHour") or 0) * 60 + (timer.get("remainMinute") or 0)
        if mins and run not in (None, "POWER_OFF", "END", "INITIAL"):
            bits.append(f"{mins} min left")
        job = next((v for k, v in part.items() if k.endswith("JobMode") and isinstance(v, dict)), {})
        if job.get("currentJobMode"):
            bits.append(_words(job["currentJobMode"]))
        temps = part.get("temperature")
        for t in temps if isinstance(temps, list) else [temps] if isinstance(temps, dict) else []:
            unit = "°" if (t.get("unit") or "F") in ("F", "C") else ""
            now, target = t.get("currentTemperature"), t.get("targetTemperature")
            where = _words(t["locationName"]) + " " if t.get("locationName") else ""
            if now is not None and target is not None:
                bits.append(f"{where}{now:g}{unit} → {target:g}{unit}")
            elif target is not None:
                bits.append(f"{where}{target:g}{unit}")
        doors = part.get("doorStatus")
        for door in doors if isinstance(doors, list) else [doors] if isinstance(doors, dict) else []:
            if door.get("doorState") == "OPEN":
                bits.append("Door open")
    return " · ".join(dict.fromkeys(bits)) or None


class ThinQAdapter(Adapter):
    brand = "thinq"
    label = "LG ThinQ"
    prefixes = ("thinq",)
    can_rename = False

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._http: httpx.AsyncClient | None = None
        self._devices: dict[str, dict] = {}    # LG device id -> device record
        self._status: dict[str, Any] = {}
        self._status_at: dict[str, float] = {}
        self._profiles: dict[str, Any] = {}
        self._listed = 0.0

    @property
    def config(self) -> dict:
        return self.secrets.get("thinq") or {}

    @property
    def active(self) -> bool:
        return bool(self.config.get("token"))

    def _client(self, cfg: dict | None = None) -> httpx.AsyncClient:
        cfg = cfg or self.config
        return httpx.AsyncClient(
            base_url=f"https://api-{region_of(cfg['country'])}.lgthinq.com", timeout=15,
            headers={"Authorization": f"Bearer {cfg['token']}", "x-country": cfg["country"],
                     "x-client-id": cfg["client_id"], "x-api-key": API_KEY, "x-service-phase": "OP"},
        )

    async def _call(self, method: str, path: str, client: httpx.AsyncClient | None = None, **kw) -> Any:
        if client is None:
            self._http = self._http or self._client()
            client = self._http
        headers = {"x-message-id": base64.urlsafe_b64encode(uuid.uuid4().bytes)[:-2].decode(), **kw.pop("headers", {})}
        try:
            r = await client.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise HubError(504, f"Couldn't reach LG ThinQ: {exc}") from exc
        data = r.json() if r.content else {}
        if r.status_code in (401, 403):
            raise HubError(401, "LG rejected the token. Make a new one at connect-pat.lgthinq.com.")
        if r.status_code >= 400:
            err = data.get("error") or {}
            raise HubError(502, f"LG ThinQ said: {err.get('message') or r.status_code}")
        return data.get("response")

    async def connect(self, token: str, country: str) -> dict:
        cfg = {"token": token.strip(), "country": country.upper(),
               "client_id": self.config.get("client_id") or f"smarthome-hub-{uuid.uuid4()}"}
        async with self._client(cfg) as client:
            found = await self._call("GET", "/devices", client=client) or []
        self.secrets.set("thinq", cfg)
        await self.close()
        self._devices, self._listed = {}, 0.0
        return {"connected": True, "devices": len(found)}

    async def disconnect(self) -> None:
        self.secrets.set("thinq", None)
        await self.close()
        self._devices = {}

    async def _list(self) -> dict[str, dict]:
        if not self._devices or time.monotonic() - self._listed > LIST_SECONDS:
            found = await self._call("GET", "/devices") or []
            self._devices = {d["deviceId"]: d for d in found if d.get("deviceId")}
            self._listed = time.monotonic()
        return self._devices

    async def _refresh(self, dev_id: str) -> None:
        if time.monotonic() - self._status_at.get(dev_id, float("-inf")) < STATUS_SECONDS:
            return
        try:
            self._status[dev_id] = await self._call("GET", f"/devices/{dev_id}/state")
            self._status_at[dev_id] = time.monotonic()
        except HubError as exc:
            log.warning("ThinQ status for %s failed: %s", dev_id, exc.message)
            self._status.pop(dev_id, None)

    def _dtype(self, dev_id: str) -> str:
        return ((self._devices.get(dev_id) or {}).get("deviceInfo") or {}).get("deviceType") or ""

    async def _profile(self, dev_id: str) -> Any:
        if dev_id not in self._profiles:
            try:
                self._profiles[dev_id] = await self._call("GET", f"/devices/{dev_id}/profile")
            except HubError as exc:
                log.warning("ThinQ profile for %s failed: %s", dev_id, exc.message)
                return None
        return self._profiles[dev_id]

    async def devices(self) -> list[Device]:
        devices = await self._list()
        laundry = [d for d in devices if self._dtype(d) in LAUNDRY]
        await asyncio.gather(*(self._refresh(d) for d in devices), *(self._profile(d) for d in laundry))
        out = []
        for dev_id, rec in devices.items():
            info = rec.get("deviceInfo") or {}
            dtype = info.get("deviceType") or "DEVICE_APPLIANCE"
            label, switchable = TYPES.get(dtype, (_words(dtype.removeprefix("DEVICE_")), False))
            status = self._status.get(dev_id)
            power = power_key(status) if switchable else None
            extra: dict[str, Any] = {}
            controls = laundry_controls(self._profiles.get(dev_id)) if dtype in LAUNDRY else None
            if controls:
                ready = remote_start_on(status)
                extra = dict(
                    ready=ready,
                    actions=[{"id": m, "label": ACTION_LABELS[m]} for m in controls["modes"]],
                    delay_max=controls["delay"][1] if controls["delay"] else None,
                )
            out.append(Device(
                id=f"thinq:{dev_id}", brand=self.brand, kind="appliance",
                name=info.get("alias") or label, model=f"{label} · {info.get('modelName') or ''}".strip(" ·"),
                online=status is not None, on=power[1] if power else None, media=summarize(status),
                capabilities=(["onoff"] if power else []) + (["actions"] if controls else []),
                **extra,
            ))
        return out

    def names(self) -> dict[str, str]:
        return {f"thinq:{d}": (r.get("deviceInfo") or {}).get("alias") or "LG appliance"
                for d, r in self._devices.items()}

    async def _control(self, dev_id: str, payload: dict) -> None:
        await self._call("POST", f"/devices/{dev_id}/control", headers={"x-conditional-control": "true"},
                         json=payload)
        self._status_at.pop(dev_id, None)

    async def _laundry(self, dev_id: str, state: StateIn) -> None:
        controls = laundry_controls(await self._profile(dev_id))
        if controls is None:
            raise HubError(400, "This LG machine can't be controlled from the hub. Use the ThinQ app.")
        # Always check the latest state: Remote Start turns itself off after a while.
        self._status_at.pop(dev_id, None)
        await self._refresh(dev_id)
        ready = remote_start_on(self._status.get(dev_id))
        if state.delay_start_hours is not None:
            if controls["delay"] is None:
                raise HubError(400, "This machine doesn't support a delayed start.")
            low, high = controls["delay"]
            if not low <= state.delay_start_hours <= high:
                raise HubError(400, f"Delayed start must be {low} to {high} hours on this machine.")
            if not ready:
                raise HubError(409, "Press Remote Start on the machine first, then try again.")
            await self._control(dev_id, {"timer": {"relativeHourToStart": state.delay_start_hours}})
        action = (state.action or "").upper() or None
        if action is None:
            return
        if action not in controls["modes"]:
            raise HubError(400, f"This machine doesn't accept {ACTION_LABELS.get(action, action)} right now.")
        if action in NEEDS_REMOTE_START and not ready:
            raise HubError(409, "Press Remote Start on the machine first, then try again.")
        await self._control(dev_id, {"operation": {controls["key"]: action}})

    async def set_state(self, device_id: str, state: StateIn) -> None:
        dev_id = device_id.split(":", 1)[1]
        rec = self._devices.get(dev_id)
        if rec is None:
            raise HubError(404, f"Unknown LG appliance {device_id}")
        if self._dtype(dev_id) in LAUNDRY:
            await self._laundry(dev_id, state)
            return
        _, switchable = TYPES.get((rec.get("deviceInfo") or {}).get("deviceType"), ("", False))
        power = power_key(self._status.get(dev_id)) if switchable else None
        if state.on is None or power is None:
            raise HubError(400, "The hub only turns LG climate devices on and off. Use the ThinQ app for this one.")
        await self._control(dev_id, {"operation": {power[0]: "POWER_ON" if state.on else "POWER_OFF"}})

    async def status(self) -> dict:
        return {"connected": self.active, "country": self.config.get("country"), "devices": len(self._devices)}

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
