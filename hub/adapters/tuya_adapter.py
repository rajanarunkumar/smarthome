"""Smart Life / Tuya devices (blinds and curtains first; also plugs, switches and lights), through the owner's own
Tuya IoT Platform project (tinytuya). No password is stored: the project's Access ID and Secret only reach the
devices linked to it, and the link can be removed in the Smart Life app or on iot.tuya.com.

One-time setup (README "Smart Life"):
1. iot.tuya.com → Cloud → Create Cloud Project: Smart Home, data center "Western America" (US).
2. In the project: Devices → Link App Account → Add App Account → scan the code with the Smart Life app
   (Me → the scan icon). Every Smart Life device then shows up in the project.
3. Copy the Access ID and Access Secret from the project's Overview into Settings → Smart Life.

The device list and each device's state come back in one call, at most once a minute (cheap on Tuya's free quota).
Blinds report position 0-100; most use 100 = fully open, and a per-device "reversed" setting covers the rest.
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

try:
    import tinytuya
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

REGIONS = {"us": "Western America", "us-e": "Eastern America", "eu": "Central Europe", "eu-w": "Western Europe",
           "in": "India", "cn": "China"}
REFRESH_SECONDS = 60
BLIND_CATEGORIES = {"cl", "clkg", "mc", "ckmkzq"}      # curtain/blind motors, curtain switches, garage-style openers
SWITCH_CATEGORIES = {"kg", "cz", "pc", "tdq", "dlq", "kt"}
LIGHT_CATEGORIES = {"dj", "dd", "xdd", "fwd", "dc", "tgq", "tgkg"}
SWITCH_CODES = ("switch_1", "switch", "switch_led", "switch_led_1")


def status_map(dev: dict) -> dict[str, Any]:
    return {s.get("code"): s.get("value") for s in dev.get("status") or [] if isinstance(s, dict)}


def blind_position(st: dict, reversed_: bool) -> int | None:
    """0 = closed, 100 = open, whatever the motor's own convention."""
    for code in ("percent_state", "percent_control", "position"):
        v = st.get(code)
        if isinstance(v, (int, float)):
            return int(100 - v) if reversed_ else int(v)
    return None


class TuyaAdapter(Adapter):
    brand = "tuya"
    label = "Smart Life"
    prefixes = ("tuya",)
    can_rename = False

    def __init__(self, secrets: Secrets, cloud_factory=None) -> None:
        self.secrets = secrets
        self._factory = cloud_factory
        self._cloud: Any = None
        self._devices: dict[str, dict] = {}
        self._at = float("-inf")
        self._lock = asyncio.Lock()
        self.error: str | None = None

    @property
    def config(self) -> dict:
        return self.secrets.get("tuya") or {}

    @property
    def active(self) -> bool:
        return AVAILABLE and bool(self.config.get("access_id"))

    def _make_cloud(self, cfg: dict) -> Any:
        if self._factory:
            return self._factory(cfg)
        return tinytuya.Cloud(apiRegion=cfg["region"], apiKey=cfg["access_id"], apiSecret=cfg["access_secret"])

    @staticmethod
    def _check(result: Any, action: str) -> Any:
        if isinstance(result, dict) and (result.get("Error") or result.get("success") is False):
            msg = str(result.get("Payload") or result.get("msg") or result.get("Error"))
            if "sign invalid" in msg or "clientId" in msg or "1004" in msg or "token invalid" in msg:
                raise HubError(401, "Tuya rejected the Access ID or Secret. Copy both from your project's Overview on iot.tuya.com.")
            if "No permissions" in msg or "1106" in msg or "28841105" in msg:
                raise HubError(403, "The Tuya project can't see this. Check the Smart Life account is linked (Devices → Link App Account) "
                                    "and the IoT Core service is still active (Cloud → Cloud Services; the free trial needs renewing).")
            raise HubError(502, f"Tuya couldn't {action}: {msg[:160]}")
        return result

    async def _fetch(self, cfg: dict | None = None) -> dict[str, dict]:
        cfg = cfg or self.config

        def load() -> list[dict]:
            cloud = self._cloud or self._make_cloud(cfg)
            self._check(getattr(cloud, "error", None) or {}, "sign in")
            raw = self._check(cloud.getdevices(verbose=True), "list devices")
            self._cloud = cloud
            return list((raw or {}).get("result") or [])
        try:
            rows = await asyncio.to_thread(load)
        except HubError as exc:
            self.error = exc.message
            raise
        except Exception as exc:  # noqa: BLE001 - network
            self.error = f"Couldn't reach Tuya: {exc}"
            raise HubError(504, self.error) from exc
        self.error = None
        return {d["id"]: d for d in rows if d.get("id")}

    async def connect(self, access_id: str, access_secret: str, region: str) -> dict:
        if not AVAILABLE:
            raise HubError(501, "Smart Life support isn't installed. Run pip install -r requirements.txt.")
        if region not in REGIONS:
            raise HubError(400, "Choose the data center your Tuya project uses.")
        cfg = {"access_id": access_id.strip(), "access_secret": access_secret.strip(), "region": region,
               "reversed": self.config.get("reversed", [])}
        self._cloud = None
        devices = await self._fetch(cfg)
        if not devices:
            raise HubError(404, "Connected, but the project has no devices. Link your Smart Life account in the project: "
                                "Devices → Link App Account, then scan the code with the Smart Life app.")
        self.secrets.set("tuya", cfg)
        self._devices, self._at = devices, time.monotonic()
        return {"connected": True, "devices": len([d for d in devices.values() if self._kind(d)])}

    async def disconnect(self) -> None:
        self.secrets.set("tuya", None)
        self._cloud, self._devices = None, {}

    async def _refresh(self) -> None:
        async with self._lock:
            if time.monotonic() - self._at >= REFRESH_SECONDS:
                self._devices = await self._fetch()
                self._at = time.monotonic()

    @staticmethod
    def _kind(dev: dict) -> str | None:
        cat = dev.get("category")
        if cat in BLIND_CATEGORIES:
            return "blind"
        if cat in LIGHT_CATEGORIES:
            return "light"
        if cat in SWITCH_CATEGORIES:
            return "plug"
        return None

    def _reversed(self, dev_id: str) -> bool:
        return dev_id in (self.config.get("reversed") or [])

    async def devices(self) -> list[Device]:
        await self._refresh()
        out = []
        for dev_id, dev in self._devices.items():
            kind = self._kind(dev)
            if kind is None:
                continue
            st = status_map(dev)
            base = dict(id=f"tuya:{dev_id}", brand=self.brand, name=dev.get("name") or "Smart Life device", kind=kind,
                        model=dev.get("product_name") or dev.get("model"), online=bool(dev.get("online", True)))
            if kind == "blind":
                pos = blind_position(st, self._reversed(dev_id))
                moving = st.get("work_state") in ("opening", "closing")
                media = (st.get("work_state").title() + "…") if moving else \
                    "Open" if pos is not None and pos >= 98 else "Closed" if pos is not None and pos <= 2 else \
                    f"{pos}% open" if pos is not None else None
                out.append(Device(**base, on=None if pos is None else pos > 2, level=pos, level_max=100, media=media,
                                  actions=[{"id": "open", "label": "Open"}, {"id": "stop", "label": "Stop"},
                                           {"id": "close", "label": "Close"}],
                                  capabilities=["onoff", "blind", "actions"] + (["position"] if pos is not None else [])))
            else:
                code = next((c for c in SWITCH_CODES if c in st), None)
                caps = ["onoff"] if code else []
                bright = st.get("bright_value_v2", st.get("bright_value"))
                if kind == "light" and isinstance(bright, (int, float)):
                    caps.append("brightness")
                out.append(Device(**base, on=bool(st.get(code)) if code else None, capabilities=caps,
                                  brightness=max(1, round(bright / (10 if "bright_value_v2" in st else 2.55))) if "brightness" in caps else None))
        return out

    def names(self) -> dict[str, str]:
        return {f"tuya:{i}": d.get("name") or "Smart Life device" for i, d in self._devices.items() if self._kind(d)}

    async def _send(self, dev_id: str, commands: list[dict]) -> None:
        def send() -> None:
            self._check(self._cloud.sendcommand(dev_id, {"commands": commands}), "change the device")
        if self._cloud is None:
            await self._refresh()
        await asyncio.to_thread(send)
        self._at = float("-inf")         # read the new state on the next look

    async def set_state(self, device_id: str, state: StateIn) -> None:
        dev_id = device_id.split(":", 1)[1]
        dev = self._devices.get(dev_id)
        if dev is None:
            await self._refresh()
            dev = self._devices.get(dev_id)
        if dev is None:
            raise HubError(404, "Unknown Smart Life device")
        kind, st = self._kind(dev), status_map(dev)
        if kind == "blind":
            action = state.action or ({True: "open", False: "close"}.get(state.on) if state.position is None else None)
            if state.position is not None:
                pos = 100 - state.position if self._reversed(dev_id) else state.position
                await self._send(dev_id, [{"code": "percent_control", "value": int(pos)}])
            elif action in ("open", "close", "stop"):
                value = {"open": "open", "close": "close", "stop": "stop"}[action]
                await self._send(dev_id, [{"code": "control", "value": value}])
            else:
                raise HubError(400, "Blinds can open, close, stop or go to a position.")
            return
        cmds = []
        if state.on is not None:
            code = next((c for c in SWITCH_CODES if c in st), None)
            if code is None:
                raise HubError(400, "This Smart Life device can't be switched from the hub.")
            cmds.append({"code": code, "value": state.on})
        if state.brightness is not None and kind == "light":
            if "bright_value_v2" in st:
                cmds.append({"code": "bright_value_v2", "value": max(10, state.brightness * 10)})
            elif "bright_value" in st:
                cmds.append({"code": "bright_value", "value": max(25, round(state.brightness * 2.55))})
        if not cmds:
            raise HubError(400, "Smart Life devices can't do that")
        await self._send(dev_id, cmds)

    def set_reversed(self, device_id: str, reversed_: bool) -> None:
        dev_id = device_id.split(":", 1)[1]
        cfg = dict(self.config)
        rev = set(cfg.get("reversed") or [])
        (rev.add if reversed_ else rev.discard)(dev_id)
        cfg["reversed"] = sorted(rev)
        self.secrets.set("tuya", cfg)

    async def status(self) -> dict:
        cfg = self.config
        return {"connected": bool(cfg.get("access_id")), "available": AVAILABLE, "region": cfg.get("region"),
                "devices": len(self.names()), "blinds": sum(1 for d in self._devices.values() if self._kind(d) == "blind"),
                "error": self.error}
