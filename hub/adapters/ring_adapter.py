"""Ring doorbells and cameras (indoor and outdoor), through Ring's cloud (ring-doorbell, the library
Home Assistant's Ring integration uses).

Sign-in: the Ring email and password, then the code Ring texts or emails (two-step verification
is always on). The hub keeps only the token Ring returns, never the password. Ring lists the hub
under Control Center → Authorized Client Devices, where it can be removed.

What the hub does: show each doorbell and camera with battery, Wi-Fi signal and the latest ring and
motion; the event history; a fresh snapshot; turn motion detection on or off; floodlight and siren on
cameras that have them. Live video isn't offered (it needs WebRTC in the app).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..config import Secrets
from ..models import Device, HubError, StateIn
from .base import Adapter

log = logging.getLogger(__name__)

try:
    from ring_doorbell import Auth, AuthenticationError, Requires2FAError, Ring, RingError
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

    class RingError(Exception):
        pass

    class AuthenticationError(RingError):
        pass

    class Requires2FAError(RingError):
        pass

USER_AGENT = "SmartHomeHub/1.0"
CODE_SECONDS = 600
REFRESH_SECONDS = 60          # device data (battery, settings)
HISTORY_SECONDS = 300         # last ring / motion per device
SIREN_SECONDS = 30


def when(value: Any) -> str | None:
    """Ring event time (datetime or ISO text) as local 'Mon 3:42 PM'."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone()
    return local.strftime("%a %-I:%M %p") if local.date() != datetime.now().astimezone().date() else local.strftime("Today %-I:%M %p")


EVENT_LABELS = {"ding": "Doorbell rang", "motion": "Motion", "on_demand": "Live view"}


def event_row(e: dict) -> dict:
    return {"kind": e.get("kind", ""), "label": EVENT_LABELS.get(e.get("kind", ""), str(e.get("kind", "Event")).replace("_", " ").title()),
            "when": when(e.get("created_at")) or "", "answered": bool(e.get("answered"))}


def device_kind(dev: Any) -> str:
    return "doorbell" if getattr(dev, "family", "") in ("doorbots", "authorized_doorbots") else "camera"


class RingAdapter(Adapter):
    brand = "ring"
    label = "Ring"
    prefixes = ("ring",)
    can_rename = False

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._ring: Any = None
        self._auth: Any = None
        self._pending: tuple[str, str, float] | None = None  # email, password (memory only), when
        self._data_at = 0.0
        self._history: dict[str, tuple[float, list[dict]]] = {}
        self._lock = asyncio.Lock()
        self.error: str | None = None

    @property
    def config(self) -> dict:
        return self.secrets.get("ring") or {}

    @property
    def active(self) -> bool:
        return AVAILABLE and bool(self.config.get("token"))

    def _save_token(self, token: dict) -> None:
        cfg = self.config
        if cfg.get("token") is not None or self._pending is not None:
            self.secrets.set("ring", {**cfg, "token": token})

    def _new_auth(self, token: dict | None) -> Any:
        return Auth(USER_AGENT, token, self._save_token, hardware_id=self.config.get("hardware_id"))

    # Sign-in: password first; Ring then sends a code, and the code finishes it.
    async def connect(self, email: str, password: str, code: str | None = None) -> dict:
        if not AVAILABLE:
            raise HubError(501, "Ring support isn't installed. Run pip install -r requirements.txt.")
        if code and self._pending is None:
            raise HubError(409, "The code expired. Enter your email and password again.")
        if code and self._pending:
            email, password, at = self._pending
            if time.monotonic() - at > CODE_SECONDS:
                self._pending = None
                raise HubError(409, "The code expired. Enter your email and password again.")
        auth = self._new_auth(None)
        try:
            token = await auth.async_fetch_token(email.strip(), password, code.strip() if code else None)
        except Requires2FAError:
            self._pending = (email.strip(), password, time.monotonic())
            await auth.async_close()
            return {"needs_code": True}
        except AuthenticationError as exc:
            await auth.async_close()
            if code:
                raise HubError(401, "Ring didn't accept that code. Check it, or start again for a new one.") from exc
            raise HubError(401, "Ring didn't accept the email or password.") from exc
        except RingError as exc:
            await auth.async_close()
            raise HubError(502, f"Ring didn't answer properly: {exc}") from exc
        self._pending = None
        self.secrets.set("ring", {"email": email.strip(), "token": token, "hardware_id": auth.get_hardware_id()})
        await self.close()
        self._auth = auth
        self._ring = Ring(auth)
        await self._update(force=True)
        return {"connected": True, "devices": len(self._video_devices())}

    async def disconnect(self) -> None:
        await self.close()
        self._history.clear()
        self.secrets.set("ring", None)

    async def _session(self) -> Any:
        if self._ring is None:
            self._auth = self._new_auth(self.config.get("token"))
            self._ring = Ring(self._auth)
        return self._ring

    async def _update(self, force: bool = False) -> None:
        async with self._lock:
            if not force and time.monotonic() - self._data_at < REFRESH_SECONDS:
                return
            ring = await self._session()
            try:
                await ring.async_update_data()
                self.error = None
            except AuthenticationError as exc:
                self.error = "Ring signed the hub out (the token was removed or expired). Connect again in Settings."
                raise HubError(401, self.error) from exc
            except RingError as exc:
                self.error = f"Ring didn't answer: {exc}"
                raise HubError(502, self.error) from exc
            self._data_at = time.monotonic()

    def _video_devices(self) -> list[Any]:
        if self._ring is None:
            return []
        devs = self._ring.devices()
        return list(devs.doorbots) + list(devs.authorized_doorbots) + list(devs.stickup_cams)

    def _find(self, device_id: str) -> Any:
        for dev in self._video_devices():
            if f"ring:{dev.device_api_id}" == device_id:
                return dev
        raise HubError(404, "Unknown Ring device")

    async def history(self, device_id: str, limit: int = 15, fresh: bool = False) -> list[dict]:
        cached = self._history.get(device_id)
        if cached and not fresh and time.monotonic() - cached[0] < HISTORY_SECONDS and len(cached[1]) >= min(limit, 5):
            return cached[1][:limit]
        await self._update()
        dev = self._find(device_id)
        try:
            events = [event_row(e) for e in await dev.async_history(limit=limit)]
        except RingError as exc:
            raise HubError(502, f"Ring didn't send the history: {exc}") from exc
        self._history[device_id] = (time.monotonic(), events)
        return events

    async def snapshot(self, device_id: str) -> bytes:
        await self._update()
        dev = self._find(device_id)
        try:
            image = await dev.async_get_snapshot()
        except RingError as exc:
            raise HubError(502, f"Ring didn't send a snapshot: {exc}") from exc
        if not image:
            raise HubError(404, "No snapshot yet. Battery cameras only take them when Snapshot Capture is on in the Ring app "
                                "(Device Settings → Video Settings), and not while they're recording.")
        return image

    async def devices(self) -> list[Device]:
        await self._update()
        out = []
        for dev in self._video_devices():
            hid = f"ring:{dev.device_api_id}"
            events = (self._history.get(hid) or (0, []))[1]
            if not events or time.monotonic() - self._history[hid][0] > HISTORY_SECONDS:
                try:
                    events = await self.history(hid, limit=15, fresh=True)
                except HubError as exc:
                    log.info("Ring history for %s: %s", dev.name, exc.message)
            last_ding = next((e for e in events if e["kind"] == "ding"), None)
            last_motion = next((e for e in events if e["kind"] == "motion"), None)
            kind = device_kind(dev)
            caps = ["events", "snapshot"]
            actions = []
            if dev.has_capability("motion_detection"):
                caps.append("motion")
                actions.append({"id": "motion", "label": f"Motion detection: {'on' if dev.motion_detection else 'off'}"})
            if dev.has_capability("light"):
                actions.append({"id": "lights", "label": f"Light: {'on' if dev.lights == 'on' else 'off'}"})
            if dev.has_capability("siren"):
                actions.append({"id": "siren_off" if dev.siren else "siren", "label": "Stop siren" if dev.siren else f"Sound siren ({SIREN_SECONDS} s)"})
            if actions:
                caps.append("actions")
            details = []
            if last_ding and kind == "doorbell":
                details.append({"label": "Last ring", "value": last_ding["when"]})
            if last_motion:
                details.append({"label": "Last motion", "value": last_motion["when"]})
            signal = getattr(dev, "wifi_signal_strength", None)
            if signal is not None:
                details.append({"label": "Wi‑Fi signal", "value": f"{signal} dBm ({getattr(dev, 'wifi_signal_category', '') or 'n/a'})"})
            if getattr(dev, "firmware", None):
                details.append({"label": "Firmware", "value": str(dev.firmware)})
            latest = events[0] if events else None
            battery = dev.battery_life if dev.has_capability("battery") else None
            offline = getattr(dev, "connection_status", None) == "offline"
            media = (f"{latest['label']} {latest['when']}" if latest else "No recent events")
            out.append(Device(id=hid, brand=self.brand, kind=kind, name=dev.name or "Ring", model=dev.model,
                              online=not offline, on=None, battery=battery if isinstance(battery, int) else None,
                              media=media, details=details, actions=actions, capabilities=caps))
        return out

    def names(self) -> dict[str, str]:
        return {f"ring:{d.device_api_id}": d.name for d in self._video_devices()}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        await self._update()
        dev = self._find(device_id)
        try:
            if state.action == "motion":
                await dev.async_set_motion_detection(not dev.motion_detection)
            elif state.action == "lights" and dev.has_capability("light"):
                await dev.async_set_lights("off" if dev.lights == "on" else "on")
            elif state.action == "siren" and dev.has_capability("siren"):
                await dev.async_set_siren(SIREN_SECONDS)
            elif state.action == "siren_off" and dev.has_capability("siren"):
                await dev.async_set_siren(0)
            else:
                raise HubError(400, "Ring devices can't do that")
        except RingError as exc:
            raise HubError(502, f"Ring didn't accept the change: {exc}") from exc
        self._data_at = 0.0   # read the new setting back on the next refresh

    async def status(self) -> dict:
        return {"connected": bool(self.config.get("token")), "available": AVAILABLE, "devices": len(self._video_devices()),
                "email": self.config.get("email"), "waiting_for_code": self._pending is not None, "error": self.error}

    async def close(self) -> None:
        if self._auth is not None:
            try:
                await self._auth.async_close()
            except Exception:  # noqa: BLE001
                pass
        self._auth = self._ring = None
        self._data_at = 0.0
