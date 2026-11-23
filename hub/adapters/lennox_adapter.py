"""Lennox iComfort S30, E30, M30 and S40 thermostats (lennoxs30api, the library Home Assistant's
Lennox integration uses).

Two ways to connect:
- iComfort account (default): the email and password of the Lennox iComfort app. Lennox's cloud needs
  the password at every reconnect, so it's kept in secrets.json (like Schlage). Use a second account
  invited to the home if you want to revoke the hub on its own.
- Local: also give the thermostat's IP address. The hub then talks to it on the Wi-Fi; no account
  traffic leaves the house (the S30/E30 accept local connections; the password is ignored).

Older iComfort Wi-Fi thermostats (before the S30, with the myicomfort.com website) use a different
service and aren't supported.

The hub keeps one connection open in the background and reads changes as they arrive, so the Rooms
view and the electricity insights (heating/cooling run time) stay current.
"""

from __future__ import annotations

import asyncio
import logging
import secrets as pysecrets
import time
from typing import Any

from ..config import Secrets
from ..models import Device, HubError, StateIn
from .base import Adapter

log = logging.getLogger(__name__)

try:
    from lennoxs30api import S30Exception, s30api_async
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

    class S30Exception(Exception):  # type: ignore[no-redef]
        error_code = 0
        message = ""

TO_LENNOX = {"HEAT": "heat", "COOL": "cool", "HEATCOOL": "heat and cool", "OFF": "off"}
FROM_LENNOX = {"heat": "HEAT", "cool": "COOL", "heat and cool": "HEATCOOL", "off": "OFF", "emergency heat": "HEAT"}
FAN_MODES = ("auto", "on", "circulate")
CLOUD_POLL_SECONDS = 10      # cloud has no long polling; LAN long-polls inside messagePump
CONNECT_TIMEOUT = 45
BACKOFF_MAX = 600
AUTH_ERRORS = {1, 2, 401}    # EC_LOGIN, EC_AUTHENTICATE, EC_UNAUTHORIZED


def explain(exc: Exception, local: bool) -> str:
    code = getattr(exc, "error_code", None)
    if code in AUTH_ERRORS and not local:
        return "Lennox didn't accept the iComfort email or password."
    if isinstance(exc, asyncio.TimeoutError):
        return ("The thermostat didn't answer in time. Check its IP address and that it's on the same network as this Mac."
                if local else "Lennox's service didn't answer in time. Try again in a few minutes.")
    msg = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return f"Lennox: {msg}"


def no_zone_detail(api: Any, local: bool) -> str:
    """Why a connection produced no zones, in terms of what Lennox actually sent."""
    systems = list(getattr(api, "system_list", []) or [])
    if not systems:
        return "Lennox accepted the sign-in but lists no thermostat on this account. Sign in with the account that set up the thermostat, or ask its owner to invite you in the iComfort app."
    parts = []
    for sy in systems:
        kind = getattr(sy, "productType", None) or "unknown model"
        status = getattr(sy, "cloud_status", None) or "no status"
        zones = len(getattr(sy, "zone_list", []) or [])
        parts.append(f"“{getattr(sy, 'name', None) or 'system'}” ({kind}, Lennox says {status}, {zones} zone(s) described)")
    head = f"Lennox accepted the sign-in and found {', '.join(parts)}, but the thermostat sent no readings in {CONNECT_TIMEOUT} s."
    if local:
        return head + " Check the IP address and that the thermostat is on the same network as this Mac."
    if any((getattr(sy, "cloud_status", "") or "").lower() == "offline" for sy in systems):
        return head + " The thermostat isn't online with Lennox: check its Wi‑Fi (Menu → Settings → Wi‑Fi) and that the iComfort app shows live temperatures."
    return head + (" Lennox's cloud often doesn't pass readings to outside apps. Connect locally instead: "
                   "enter the thermostat's IP address (Menu → Settings → Wi‑Fi → Advanced on the thermostat) with the email left as is.")


def zone_state(zone: Any, system: Any) -> dict:
    op = (zone.tempOperation or "off").lower()
    return {
        "current": round(zone.temperature) if zone.temperature is not None else None,
        "humidity": round(zone.humidity) if zone.humidity is not None else None,
        "mode": FROM_LENNOX.get((zone.systemMode or "off").lower(), "OFF"),
        "modes": [m for m, ok in (("HEAT", zone.heatingOption is not False), ("COOL", zone.coolingOption is not False),
                                  ("HEATCOOL", zone.heatingOption is not False and zone.coolingOption is not False), ("OFF", True)) if ok],
        "hvac": {"heating": "HEATING", "cooling": "COOLING"}.get(op, "OFF"),
        "heat": round(zone.hsp) if zone.hsp is not None else None,
        "cool": round(zone.csp) if zone.csp is not None else None,
        "eco": None,
        "fan": None,                      # Lennox fan modes are offered as buttons instead of a timer
        "fan_mode": zone.fanMode,
        "emergency": (zone.systemMode or "").lower() == "emergency heat",
        "single_setpoint": bool(getattr(system, "single_setpoint_mode", False)),
    }


def summary(s: dict) -> str:
    action = {"HEATING": "Heating", "COOLING": "Cooling"}.get(s["hvac"])
    if s["mode"] == "OFF":
        head = "Off"
    elif s["mode"] == "HEATCOOL":
        head = f"{action or 'Holding'} {s['heat']}–{s['cool']}°"
    else:
        target = s["heat"] if s["mode"] == "HEAT" else s["cool"]
        head = f"{action} to {target}°" if action else f"{s['mode'].title()} {target}°"
    if s["emergency"]:
        head = "Emergency heat · " + head
    return f"{head} · {s['current']}° inside" if s["current"] is not None else head


class LennoxAdapter(Adapter):
    brand = "lennox"
    label = "Lennox"
    prefixes = ("lennox",)
    can_rename = False

    def __init__(self, secrets: Secrets, factory=None) -> None:
        self.secrets = secrets
        self._factory = factory or (lambda **kw: s30api_async(**kw))
        self._api: Any = None
        self._task: asyncio.Task | None = None
        self._last_mode: dict[str, str] = {}
        self.error: str | None = None
        self.connected_at: float | None = None

    @property
    def config(self) -> dict:
        return self.secrets.get("lennox") or {}

    @property
    def active(self) -> bool:
        return AVAILABLE and bool(self.config.get("email") or self.config.get("ip"))

    def _make_api(self, cfg: dict) -> Any:
        return self._factory(username=cfg.get("email") or "", password=cfg.get("password") or "", app_id=cfg["app_id"],
                             ip_address=cfg.get("ip") or None, pii_message_logs=False, message_debug_logging=False)

    async def _open(self, cfg: dict) -> Any:
        """Connect, subscribe to every system, and read until the zones are known."""
        api = self._make_api(cfg)
        try:
            await asyncio.wait_for(api.serverConnect(), CONNECT_TIMEOUT)
            for system in api.system_list:
                await asyncio.wait_for(api.subscribe(system), CONNECT_TIMEOUT)
            deadline = time.monotonic() + CONNECT_TIMEOUT
            while not self._zones(api) and time.monotonic() < deadline:
                await api.messagePump()
                if not cfg.get("ip"):
                    await asyncio.sleep(1)
        except BaseException:
            try:
                await api.shutdown()
            except Exception:  # noqa: BLE001
                pass
            raise
        return api

    @staticmethod
    def _zones(api: Any) -> list[tuple[Any, Any]]:
        out = []
        for system in getattr(api, "system_list", []):
            for zone in system.zone_list:
                if zone.is_zone_active() and zone.temperature is not None:
                    out.append((system, zone))
        return out

    async def start(self) -> None:
        if self.active and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        delay = 30
        while True:
            cfg = self.config
            if not (cfg.get("email") or cfg.get("ip")):
                return
            try:
                if self._api is None:
                    self._api = await self._open(cfg)
                    self.error, self.connected_at, delay = None, time.time(), 30
                began = time.monotonic()
                await self._api.messagePump()
                # Cloud has no long polling, so wait between reads. Locally the thermostat holds each read open
                # for up to 15 s; if it ever answers instantly, still pause so the hub never spins on it.
                pause = CLOUD_POLL_SECONDS if not cfg.get("ip") else 1 - (time.monotonic() - began)
                await asyncio.sleep(max(pause, 0))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.error = explain(exc, bool(cfg.get("ip")))
                log.warning("Lennox: %s", self.error)
                await self._drop()
                if getattr(exc, "error_code", None) in AUTH_ERRORS and not cfg.get("ip"):
                    delay = BACKOFF_MAX             # don't keep sending a rejected password
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)

    async def _drop(self) -> None:
        api, self._api = self._api, None
        if api is not None:
            try:
                await api.shutdown()
            except Exception:  # noqa: BLE001
                pass

    async def connect(self, email: str, password: str, ip: str | None) -> dict:
        if not AVAILABLE:
            raise HubError(501, "Lennox support isn't installed. Run pip install -r requirements.txt.")
        if not ip and not (email and password):
            raise HubError(400, "Enter the iComfort email and password, or the thermostat's IP address.")
        cfg = {"email": email.strip(), "password": password, "ip": (ip or "").strip() or None,
               "app_id": self.config.get("app_id") or "smarthomehub" + pysecrets.token_hex(6)}
        await self.close()
        try:
            api = await self._open(cfg)
        except Exception as exc:  # noqa: BLE001
            message = explain(exc, bool(cfg["ip"]))
            raise HubError(401 if getattr(exc, "error_code", None) in AUTH_ERRORS else 504, message) from exc
        if not self._zones(api):
            detail = no_zone_detail(api, bool(cfg["ip"]))
            log.warning("Lennox: %s", detail)
            await api.shutdown()
            raise HubError(404, detail)
        self.secrets.set("lennox", cfg)
        self._api, self.error, self.connected_at = api, None, time.time()
        self._task = asyncio.create_task(self._run())
        return {"connected": True, "devices": len(self._zones(api))}

    async def disconnect(self) -> None:
        await self.close()
        self.secrets.set("lennox", None)

    @staticmethod
    def _hub_id(system: Any, zone: Any) -> str:
        return f"lennox:{system.sysId}:{zone.id}"

    def _find(self, device_id: str) -> tuple[Any, Any]:
        if self._api is None:
            raise HubError(503, self.error or "Connecting to Lennox…")
        for system, zone in self._zones(self._api):
            if self._hub_id(system, zone) == device_id:
                return system, zone
        raise HubError(404, "Unknown Lennox zone")

    def _name(self, system: Any, zone: Any, many: bool) -> str:
        base = system.name or "Lennox"
        return f"{base} {zone.name}" if many and zone.name else (zone.name if many else f"{base} Thermostat")

    async def devices(self) -> list[Device]:
        if self._api is None:
            return []
        zones = self._zones(self._api)
        out = []
        for system, zone in zones:
            s = zone_state(zone, system)
            hid = self._hub_id(system, zone)
            if s["mode"] != "OFF":
                self._last_mode[hid] = s["mode"]
            details = []
            if system.outdoorTemperature is not None:
                details.append({"label": "Outside", "value": f"{round(system.outdoorTemperature)}°"})
            fan = (s["fan_mode"] or "").lower()
            actions = [{"id": f"fan_{m}", "label": f"Fan {m}{' ✓' if m == fan else ''}"} for m in FAN_MODES]
            online = getattr(system, "cloud_status", "online") in (None, "online")
            out.append(Device(id=hid, brand=self.brand, kind="thermostat", name=self._name(system, zone, len(zones) > 1),
                              model=f"iComfort {system.productType or ''}".strip(), online=online,
                              on=s["hvac"] in ("HEATING", "COOLING"), media=summary(s), details=details, actions=actions,
                              thermostat=s, capabilities=["thermostat", "actions"]))
        return out

    def names(self) -> dict[str, str]:
        if self._api is None:
            return {}
        zones = self._zones(self._api)
        return {self._hub_id(s, z): self._name(s, z, len(zones) > 1) for s, z in zones}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        system, zone = self._find(device_id)
        s = zone_state(zone, system)
        mode = state.thermostat_mode
        if state.on is False:
            mode = "OFF"
        elif state.on is True and s["mode"] == "OFF":
            mode = self._last_mode.get(device_id) or next((m for m in ("HEAT", "COOL", "HEATCOOL") if m in s["modes"]), "HEAT")
        try:
            if mode and mode != s["mode"]:
                if mode not in s["modes"]:
                    raise HubError(400, f"This system doesn't offer {mode.title()} mode.")
                await zone.setHVACMode(TO_LENNOX[mode])
                s["mode"] = mode
            if state.heat_f is not None or state.cool_f is not None:
                if s["mode"] == "OFF":
                    raise HubError(409, "The system is off. Choose Heat, Cool or Heat·Cool first.")
                if s["single_setpoint"]:
                    await zone.perform_setpoint(r_sp=state.heat_f if state.heat_f is not None else state.cool_f)
                elif s["mode"] == "HEATCOOL" and state.heat_f is not None and state.cool_f is not None:
                    await zone.perform_setpoint(r_hsp=state.heat_f, r_csp=state.cool_f)
                elif state.heat_f is not None and s["mode"] in ("HEAT", "HEATCOOL"):
                    await zone.perform_setpoint(r_hsp=state.heat_f)     # the library keeps cool 3° above, like the app
                elif state.cool_f is not None and s["mode"] in ("COOL", "HEATCOOL"):
                    await zone.perform_setpoint(r_csp=state.cool_f)
                else:
                    raise HubError(400, f"In {s['mode'].title()} mode, set the {'heating' if s['mode'] == 'HEAT' else 'cooling'} temperature.")
            if state.action:
                fan = state.action.removeprefix("fan_")
                if not state.action.startswith("fan_") or fan not in FAN_MODES:
                    raise HubError(400, "Unknown Lennox command")
                await zone.setFanMode(fan)
        except S30Exception as exc:
            raise HubError(400, explain(exc, bool(self.config.get("ip")))) from exc

    async def status(self) -> dict:
        cfg = self.config
        return {"connected": bool(cfg.get("email") or cfg.get("ip")), "available": AVAILABLE,
                "local": bool(cfg.get("ip")), "online": self._api is not None, "error": self.error,
                "devices": len(self._zones(self._api)) if self._api is not None else 0}

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._drop()
