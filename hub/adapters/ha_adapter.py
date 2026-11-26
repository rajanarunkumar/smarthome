"""Home Assistant as the device layer (docs/PLAN-HOME-ASSISTANT.md).

The hub keeps one WebSocket open to HA: it reads every entity's state once, HA's area/device/entity registries
(which integration each entity comes from, and its area), then receives every state change as it happens.
No polling. Commands go back as HA service calls.

Brands are moved one at a time: while a brand's "source" is Home Assistant, the hub's own connector for it is
switched off and its devices come from HA's entities of the matching integration(s) instead (ids "ha:<entity_id>").
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from ..config import Secrets
from ..models import Device, HubError, StateIn
from .base import Adapter

log = logging.getLogger(__name__)

# The hub's brands and the HA integrations (platforms) that replace them.
BRAND_PLATFORMS: dict[str, tuple[str, ...]] = {
    "hue": ("hue",), "kasa": ("tplink",), "sonos": ("sonos",), "lg": ("webostv",), "govee": ("govee_light_local", "govee"),
    "thinq": ("lg_thinq",), "dyson": ("dyson_local",), "ecovacs": ("ecovacs",), "schlage": ("schlage",), "ring": ("ring",),
    "nest": ("nest",), "lennox": ("lennoxs30",), "tuya": ("tuya",), "rainbird": ("rainbird",), "pentair": ("screenlogic",),
}
CONTROL_DOMAINS = {"light", "switch", "cover", "climate", "lock", "media_player", "vacuum", "fan", "camera"}
KIND = {"light": "light", "switch": "plug", "cover": "blind", "climate": "thermostat", "lock": "lock",
        "media_player": "speaker", "vacuum": "vacuum", "fan": "purifier", "camera": "camera"}
HVAC_TO_HUB = {"heat": "HEAT", "cool": "COOL", "heat_cool": "HEATCOOL", "auto": "HEATCOOL", "off": "OFF"}
HUB_TO_HVAC = {"HEAT": "heat", "COOL": "cool", "HEATCOOL": "heat_cool", "OFF": "off"}
BACKOFF_MAX = 300


def norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class HomeAssistant(Adapter):
    brand = "ha"
    label = "Home Assistant"
    prefixes = ("ha",)
    can_rename = False

    def __init__(self, secrets: Secrets, session_factory=None) -> None:
        self.secrets = secrets
        self._session_factory = session_factory
        self.states: dict[str, dict] = {}
        self.entities: dict[str, dict] = {}      # entity registry: entity_id -> {platform, device_id, area_id, name}
        self.devices_reg: dict[str, dict] = {}   # device registry: id -> {name, area_id, model, manufacturer}
        self.areas: dict[str, str] = {}          # area_id -> name
        self.connected = False
        self.version: str | None = None
        self.error: str | None = None
        self.moved: set[str] = set()             # hub brands whose devices come from HA (set by the hub)
        self.on_change = None                    # callback(entity_id, old_state, new_state), set by the hub
        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self._next = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._ready = asyncio.Event()

    # ---- configuration ----
    @property
    def config(self) -> dict:
        return self.secrets.get("home_assistant") or {}

    @property
    def active(self) -> bool:
        return bool(self.config.get("token"))

    def _session(self):
        import aiohttp
        return self._session_factory() if self._session_factory else aiohttp.ClientSession()

    async def test(self, url: str, token: str) -> dict:
        """Check the address and token with HA's REST API; returns HA's version and location name."""
        import aiohttp
        url = url.rstrip("/")
        try:
            async with self._session() as session:
                async with session.get(f"{url}/api/config", headers={"Authorization": f"Bearer {token}"},
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 401:
                        raise HubError(400, "Home Assistant rejected the token. Make a long-lived access token in HA: your profile → Security.")
                    if resp.status != 200:
                        raise HubError(502, f"Home Assistant answered {resp.status} at {url}.")
                    cfg = await resp.json()
        except HubError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HubError(504, f"Can't reach Home Assistant at {url}. Check the address (e.g. http://homeassistant.local:8123) "
                                "and that the virtual machine is running.") from exc
        return {"version": cfg.get("version"), "name": cfg.get("location_name")}

    async def connect(self, url: str, token: str) -> dict:
        info = await self.test(url, token)
        self.secrets.set("home_assistant", {"url": url.rstrip("/"), "token": token})
        await self.close()
        await self.start()
        try:
            await asyncio.wait_for(self._ready.wait(), 20)
        except asyncio.TimeoutError:
            pass
        return {**info, "connected": self.connected, "entities": len(self.states)}

    async def disconnect(self) -> None:
        await self.close()
        self.secrets.set("home_assistant", None)
        self.states.clear()

    # ---- the WebSocket ----
    async def start(self) -> None:
        if self.active and (self._task is None or self._task.done()):
            self._ready = asyncio.Event()
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        delay = 5
        while self.active:
            try:
                await self._session_once()
                delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.error = f"Lost the connection to Home Assistant: {exc}"
                log.warning("Home Assistant: %s", exc)
            self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX)

    async def _session_once(self) -> None:
        cfg = self.config
        ws_url = re.sub(r"^http", "ws", cfg["url"]) + "/api/websocket"
        async with self._session() as session:
            async with session.ws_connect(ws_url, heartbeat=30, max_msg_size=0) as ws:
                hello = await ws.receive_json()
                if hello.get("type") != "auth_required":
                    raise RuntimeError(f"unexpected greeting {hello.get('type')}")
                await ws.send_json({"type": "auth", "access_token": cfg["token"]})
                auth = await ws.receive_json()
                if auth.get("type") != "auth_ok":
                    self.error = "Home Assistant rejected the token. Make a new long-lived access token and connect again."
                    raise RuntimeError("auth_invalid")
                self.version = auth.get("ha_version")
                self._ws = ws
                reader = asyncio.create_task(self._read(ws))
                try:
                    states, ents, devs, areas = await asyncio.gather(
                        self._call({"type": "get_states"}), self._call({"type": "config/entity_registry/list"}),
                        self._call({"type": "config/device_registry/list"}), self._call({"type": "config/area_registry/list"}))
                    self.states = {s["entity_id"]: s for s in states or []}
                    self.entities = {e["entity_id"]: e for e in ents or []}
                    self.devices_reg = {d["id"]: d for d in devs or []}
                    self.areas = {a["area_id"]: a["name"] for a in areas or []}
                    await self._call({"type": "subscribe_events", "event_type": "state_changed"})
                    self.connected, self.error = True, None
                    self._ready.set()
                    await reader
                finally:
                    reader.cancel()
                    self._ws = None
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.set_exception(HubError(503, "Home Assistant disconnected"))
                    self._pending.clear()

    async def _read(self, ws) -> None:
        import aiohttp
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            data = msg.json()
            for item in data if isinstance(data, list) else [data]:
                self._handle(item)

    def _handle(self, item: dict) -> None:
        if item.get("type") == "result":
            fut = self._pending.pop(item.get("id"), None)
            if fut and not fut.done():
                if item.get("success"):
                    fut.set_result(item.get("result"))
                else:
                    err = item.get("error") or {}
                    fut.set_exception(HubError(400, f"Home Assistant: {err.get('message') or err.get('code') or 'failed'}"))
        elif item.get("type") == "event":
            ev = (item.get("event") or {}).get("data") or {}
            eid, new = ev.get("entity_id"), ev.get("new_state")
            if not eid:
                return
            old = self.states.get(eid)
            if new is None:
                self.states.pop(eid, None)
            else:
                self.states[eid] = new
            if self.on_change:
                try:
                    self.on_change(eid, old, new)
                except Exception as exc:  # noqa: BLE001
                    log.debug("on_change: %s", exc)

    async def _call(self, msg: dict, timeout: float = 20) -> Any:
        if self._ws is None:
            raise HubError(503, self.error or "Not connected to Home Assistant yet.")
        mid = self._next
        self._next += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send_json({**msg, "id": mid})
        return await asyncio.wait_for(fut, timeout)

    async def call_service(self, domain: str, service: str, entity_id: str, data: dict | None = None) -> None:
        await self._call({"type": "call_service", "domain": domain, "service": service,
                          "target": {"entity_id": entity_id}, "service_data": data or {}})

    # ---- entities ----
    def platform(self, entity_id: str) -> str | None:
        return (self.entities.get(entity_id) or {}).get("platform")

    def area(self, entity_id: str) -> str | None:
        e = self.entities.get(entity_id) or {}
        area_id = e.get("area_id") or (self.devices_reg.get(e.get("device_id")) or {}).get("area_id")
        return self.areas.get(area_id) if area_id else None

    def name(self, entity_id: str) -> str:
        st = self.states.get(entity_id) or {}
        return (st.get("attributes") or {}).get("friendly_name") or (self.entities.get(entity_id) or {}).get("name") or entity_id

    def entities_for(self, brand: str) -> list[str]:
        platforms = BRAND_PLATFORMS.get(brand, ())
        return sorted(eid for eid in self.states if eid.split(".", 1)[0] in CONTROL_DOMAINS and self.platform(eid) in platforms)

    def _brand_of(self, entity_id: str) -> str | None:
        p = self.platform(entity_id)
        return next((b for b, ps in BRAND_PLATFORMS.items() if p in ps), None)

    async def devices(self) -> list[Device]:
        out = []
        for eid in sorted(self.states):
            domain = eid.split(".", 1)[0]
            if domain not in CONTROL_DOMAINS or self._brand_of(eid) not in self.moved:
                continue
            out.append(self._to_device(eid))
        return out

    def _to_device(self, eid: str) -> Device:
        st = self.states[eid]
        a = st.get("attributes") or {}
        domain, state = eid.split(".", 1)[0], st.get("state")
        online = state not in ("unavailable", "unknown")
        dev = dict(id=f"ha:{eid}", brand=self._brand_of(eid) or "ha", name=self.name(eid), kind=KIND[domain],
                   model=(self.devices_reg.get((self.entities.get(eid) or {}).get("device_id")) or {}).get("model"), online=online)
        if domain in ("light", "switch", "fan"):
            caps = ["onoff"]
            bri = a.get("brightness")
            modes = set(a.get("supported_color_modes") or [])
            if domain == "light" and (bri is not None or modes - {"onoff"}):
                caps.append("brightness")
            return Device(**dev, on=state == "on", brightness=round(bri / 2.55) or 1 if bri else None, capabilities=caps,
                          watts=a.get("current_power_w"))
        if domain == "cover":
            pos = a.get("current_position")
            moving = state in ("opening", "closing")
            media = state.title() + "…" if moving else "Open" if state == "open" and (pos is None or pos >= 98) else \
                "Closed" if state == "closed" else f"{pos}% open" if pos is not None else state
            return Device(**dev, on=state in ("open", "opening"), level=pos, level_max=100, media=media,
                          actions=[{"id": "open", "label": "Open"}, {"id": "stop", "label": "Stop"}, {"id": "close", "label": "Close"}],
                          capabilities=["onoff", "blind", "actions"] + (["position"] if pos is not None else []))
        if domain == "climate":
            hvac = a.get("hvac_action") or ""
            t = {"current": a.get("current_temperature"), "humidity": a.get("current_humidity"),
                 "mode": HVAC_TO_HUB.get(state, "OFF"), "modes": [HVAC_TO_HUB[m] for m in a.get("hvac_modes") or [] if m in HVAC_TO_HUB],
                 "hvac": "HEATING" if hvac == "heating" else "COOLING" if hvac == "cooling" else "OFF",
                 "heat": a.get("target_temp_low") or (a.get("temperature") if state == "heat" else None),
                 "cool": a.get("target_temp_high") or (a.get("temperature") if state == "cool" else None), "eco": None, "fan": None}
            return Device(**dev, on=t["hvac"] in ("HEATING", "COOLING"), thermostat=t, capabilities=["thermostat"],
                          media=f"{t['hvac'].title() if t['hvac'] != 'OFF' else t['mode'].title()} · {t['current']}° inside")
        if domain == "lock":
            return Device(**dev, on=state == "locked", media=state.title(), capabilities=["lock"])
        if domain == "media_player":
            return Device(**dev, on=state not in ("off", "standby"), playing=state == "playing", media=a.get("media_title"),
                          volume=round((a.get("volume_level") or 0) * 100) if a.get("volume_level") is not None else None,
                          capabilities=["onoff", "volume", "playback"])
        if domain == "vacuum":
            return Device(**dev, on=state == "cleaning", media=state.title(), battery=a.get("battery_level"),
                          actions=[{"id": "clean", "label": "Clean"}, {"id": "dock", "label": "Dock"}], capabilities=["actions"])
        return Device(**dev, media=state, capabilities=[])

    def names(self) -> dict[str, str]:
        return {f"ha:{eid}": self.name(eid) for eid in self.states
                if eid.split(".", 1)[0] in CONTROL_DOMAINS and self._brand_of(eid) in self.moved}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        eid = device_id.split(":", 1)[1]
        domain = eid.split(".", 1)[0]
        if eid not in self.states:
            raise HubError(404, "Unknown Home Assistant entity")
        if domain == "lock":
            if state.locked is None:
                raise HubError(400, "Locks lock or unlock.")
            return await self.call_service("lock", "lock" if state.locked else "unlock", eid)
        if domain == "cover":
            if state.position is not None:
                return await self.call_service("cover", "set_cover_position", eid, {"position": state.position})
            action = state.action or {True: "open", False: "close"}.get(state.on)
            if action in ("open", "close", "stop"):
                return await self.call_service("cover", {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"}[action], eid)
            raise HubError(400, "Blinds open, close, stop or go to a position.")
        if domain == "climate":
            if state.thermostat_mode:
                await self.call_service("climate", "set_hvac_mode", eid, {"hvac_mode": HUB_TO_HVAC[state.thermostat_mode]})
            if state.heat_f is not None and state.cool_f is not None:
                await self.call_service("climate", "set_temperature", eid, {"target_temp_low": state.heat_f, "target_temp_high": state.cool_f})
            elif state.heat_f is not None or state.cool_f is not None:
                await self.call_service("climate", "set_temperature", eid, {"temperature": state.heat_f if state.heat_f is not None else state.cool_f})
            return
        if domain == "vacuum" and state.action in ("clean", "dock"):
            return await self.call_service("vacuum", "start" if state.action == "clean" else "return_to_base", eid)
        if domain == "media_player" and state.volume is not None:
            return await self.call_service("media_player", "volume_set", eid, {"volume_level": state.volume / 100})
        if domain == "media_player" and state.playback:
            return await self.call_service("media_player", {"play": "media_play", "pause": "media_pause", "next": "media_next_track",
                                                            "previous": "media_previous_track"}[state.playback], eid)
        if state.brightness is not None and domain == "light":
            return await self.call_service("light", "turn_on", eid, {"brightness_pct": state.brightness})
        if state.on is not None:
            return await self.call_service(domain if domain in ("light", "switch", "fan", "media_player") else "homeassistant",
                                           "turn_on" if state.on else "turn_off", eid)
        raise HubError(400, "Home Assistant devices can't do that from the hub; use the HA app.")

    async def status(self) -> dict:
        cfg = self.config
        return {"connected": bool(cfg.get("token")), "online": self.connected, "url": cfg.get("url"),
                "version": self.version, "entities": len(self.states), "error": self.error, "devices": len(self.names())}

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.connected = False
