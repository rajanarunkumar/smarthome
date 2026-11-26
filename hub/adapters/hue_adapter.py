"""Philips Hue Bridge over its local API.

Lights, rooms, scenes and colors use the CLIP v2 API. Hub-created schedules use
the bridge's v1 schedules (they run on the bridge). Hue app automations
(v2 behavior instances) are listed and can be enabled or disabled.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

import httpx
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from ..color import hex_to_xy, mirek_to_hex, xy_to_hex
from ..config import Secrets
from ..models import Device, HubError, Scene, Schedule, ScheduleIn, StateIn, Trigger
from .base import Adapter
from ..schedule_codec import HUB_MARK, build_hue_schedule, parse_hue_command, parse_hue_localtime

log = logging.getLogger(__name__)
WEEKDAYS = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
            "friday": "fri", "saturday": "sat", "sunday": "sun"}


async def discover_bridges(timeout: float = 4.0) -> list[dict]:
    """Find Hue bridges on the LAN with mDNS (_hue._tcp)."""
    found: dict[str, dict] = {}
    azc = AsyncZeroconf()
    names: list[str] = []

    def on_change(zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Added:
            names.append(name)

    browser = AsyncServiceBrowser(azc.zeroconf, ["_hue._tcp.local."], handlers=[on_change])
    await asyncio.sleep(timeout)
    for name in names:
        info = AsyncServiceInfo("_hue._tcp.local.", name)
        if await info.async_request(azc.zeroconf, 3000):
            ipv4 = [socket.inet_ntoa(a) for a in info.addresses if len(a) == 4]
            bridge_id = (info.properties.get(b"bridgeid") or b"").decode()
            if ipv4:
                found[bridge_id or ipv4[0]] = {"host": ipv4[0], "bridge_id": bridge_id}
    await browser.async_cancel()
    await azc.async_close()
    return list(found.values())


class HueAdapter(Adapter):
    brand = "hue"
    label = "Hue"
    prefixes = ("hue", "huev1", "hueapp")

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._client: httpx.AsyncClient | None = None
        self._light_owner: dict[str, str] = {}   # light id -> device id
        self._light_v1: dict[str, str] = {}      # light id -> "/lights/N"
        self._names: dict[str, str] = {}         # hub id -> name
        self.room_hints: dict[str, str] = {}     # hub id -> Hue room name

    # Connection and pairing
    @property
    def config(self) -> dict:
        return self.secrets.get("hue") or {}

    @property
    def active(self) -> bool:
        return self.paired

    @property
    def paired(self) -> bool:
        return bool(self.config.get("app_key") and self.config.get("host"))

    def _http(self) -> httpx.AsyncClient:
        if not self.paired:
            raise HubError(409, "Hue Bridge is not paired yet. Pair it in Settings.")
        if self._client is None:
            # The bridge uses a certificate signed by Signify's own CA, not a public one.
            self._client = httpx.AsyncClient(
                base_url=f"https://{self.config['host']}",
                headers={"hue-application-key": self.config["app_key"]},
                verify=False,
                timeout=8,
            )
        return self._client

    async def pair(self) -> dict:
        """Create an app key. The bridge's link button must be pressed first."""
        bridges = await discover_bridges()
        if not bridges:
            raise HubError(404, "No Hue Bridge found on this network.")
        bridge = bridges[0]
        async with httpx.AsyncClient(verify=False, timeout=8) as client:
            resp = await client.post(
                f"https://{bridge['host']}/api",
                json={"devicetype": "smarthome_hub#mac", "generateclientkey": True},
            )
        result = resp.json()[0]
        if "error" in result:
            if result["error"].get("type") == 101:
                raise HubError(409, "Press the round link button on the Hue Bridge, then tap Pair within 30 seconds.")
            raise HubError(502, result["error"].get("description", "Hue pairing failed"))
        self.secrets.set("hue", {
            **bridge,
            "app_key": result["success"]["username"],
            "client_key": result["success"].get("clientkey"),
        })
        await self.close()
        return {"paired": True, "bridge_id": bridge["bridge_id"]}

    async def status(self) -> dict:
        return {"paired": self.paired, "bridge_id": self.config.get("bridge_id")}

    # CLIP v2 helpers
    async def _get(self, resource: str) -> list[dict]:
        resp = await self._http().get(f"/clip/v2/resource/{resource}")
        if resp.status_code == 403:
            raise HubError(409, "The Hue Bridge rejected the hub's key. Pair it again in Settings.")
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def _put(self, resource: str, rid: str, body: dict) -> None:
        resp = await self._http().put(f"/clip/v2/resource/{resource}/{rid}", json=body)
        if resp.status_code >= 400:
            errors = resp.json().get("errors") if resp.content else None
            raise HubError(502, f"Hue rejected the change: {errors or resp.status_code}")

    # v1 helpers (schedules)
    async def _v1(self, method: str, path: str, body: dict | None = None) -> Any:
        key = self.config["app_key"]
        resp = await self._http().request(method, f"/api/{key}{path}", json=body)
        data = resp.json()
        if isinstance(data, list) and data and "error" in data[0]:
            raise HubError(502, f"Hue: {data[0]['error'].get('description')}")
        return data

    @staticmethod
    def _rid(hub_id: str) -> str:
        return hub_id.split(":", 1)[1]

    # Devices
    async def devices(self) -> list[Device]:
        lights, devices, rooms, conn = await asyncio.gather(
            self._get("light"), self._get("device"), self._get("room"), self._get("zigbee_connectivity")
        )
        dev_by_id = {d["id"]: d for d in devices}
        offline = {c["owner"]["rid"] for c in conn if c.get("status") != "connected"}
        room_of_device = {
            child["rid"]: room["metadata"]["name"]
            for room in rooms for child in room.get("children", []) if child.get("rtype") == "device"
        }
        out: list[Device] = []
        self.room_hints.clear()
        for light in lights:
            owner_id = light["owner"]["rid"]
            owner = dev_by_id.get(owner_id, {})
            hub_id = f"hue:{light['id']}"
            name = owner.get("metadata", {}).get("name") or light["metadata"]["name"]
            self._light_owner[light["id"]] = owner_id
            if light.get("id_v1"):
                self._light_v1[light["id"]] = light["id_v1"]
            self._names[hub_id] = name
            if owner_id in room_of_device:
                self.room_hints[hub_id] = room_of_device[owner_id]

            caps = ["onoff", "schedule"]
            ct = light.get("color_temperature") or {}
            schema = ct.get("mirek_schema") or {}
            color = None
            if "dimming" in light:
                caps.append("brightness")
            if "color" in light:
                caps.append("color")
            if ct:
                caps.append("color_temp")
            if ct.get("mirek_valid") and ct.get("mirek"):
                color = mirek_to_hex(ct["mirek"])
            elif "color" in light:
                xy = light["color"]["xy"]
                color = xy_to_hex(xy["x"], xy["y"])

            out.append(Device(
                id=hub_id,
                brand=self.brand,
                name=name,
                model=owner.get("product_data", {}).get("product_name"),
                model_id=owner.get("product_data", {}).get("model_id"),
                kind="light",
                online=owner_id not in offline,
                on=light["on"]["on"],
                brightness=round(light["dimming"]["brightness"]) or 1 if "dimming" in light else None,
                color=color,
                mirek=ct.get("mirek") if ct.get("mirek_valid") else None,
                mirek_min=schema.get("mirek_minimum"),
                mirek_max=schema.get("mirek_maximum"),
                capabilities=caps,
            ))
        return out

    def names(self) -> dict[str, str]:
        return dict(self._names)

    async def set_state(self, hub_id: str, state: StateIn) -> None:
        body: dict = {}
        if state.on is not None:
            body["on"] = {"on": state.on}
        if state.brightness is not None:
            body["dimming"] = {"brightness": state.brightness}
            body.setdefault("on", {"on": True})
        if state.color is not None:
            x, y = hex_to_xy(state.color)
            body["color"] = {"xy": {"x": x, "y": y}}
            body.setdefault("on", {"on": True})
        if state.mirek is not None:
            body["color_temperature"] = {"mirek": state.mirek}
            body.setdefault("on", {"on": True})
        if body:
            await self._put("light", self._rid(hub_id), body)

    async def rename(self, hub_id: str, name: str) -> None:
        rid = self._rid(hub_id)
        if rid not in self._light_owner:
            await self.devices()
        owner = self._light_owner.get(rid)
        if owner is None:
            raise HubError(404, "Unknown Hue light")
        await self._put("device", owner, {"metadata": {"name": name}})
        self._names[hub_id] = name

    # Scenes
    async def scenes(self) -> list[Scene]:
        scenes, rooms, zones = await asyncio.gather(self._get("scene"), self._get("room"), self._get("zone"))
        group_names = {g["id"]: g["metadata"]["name"] for g in rooms + zones}
        out = []
        for s in scenes:
            palette = s.get("palette") or {}
            out.append(Scene(
                id=f"hue:{s['id']}",
                brand=self.brand,
                name=s["metadata"]["name"],
                group=group_names.get(s.get("group", {}).get("rid")),
                active=(s.get("status") or {}).get("active", "inactive") != "inactive",
                dynamic=bool(palette.get("color") or palette.get("color_temperature")),
            ))
        return sorted(out, key=lambda sc: ((sc.group or "~"), sc.name.lower()))

    async def activate_scene(self, scene_id: str, dynamic: bool) -> None:
        action = "dynamic_palette" if dynamic else "active"
        await self._put("scene", self._rid(scene_id), {"recall": {"action": action}})

    # Schedules
    async def schedules(self) -> list[Schedule]:
        if not self._light_v1:
            await self.devices()
        v1_to_hub = {v1: f"hue:{lid}" for lid, v1 in self._light_v1.items()}
        raw, behaviors = await asyncio.gather(self._v1("GET", "/schedules"), self._get("behavior_instance"))
        out: list[Schedule] = []
        for sid, s in (raw or {}).items():
            out.append(self._v1_to_schedule(sid, s, v1_to_hub))
        for b in behaviors:
            out.append(self._behavior_to_schedule(b))
        return out

    def _v1_to_schedule(self, sid: str, s: dict, v1_to_hub: dict[str, str]) -> Schedule:
        timing = parse_hue_localtime(s.get("localtime", ""))
        command = parse_hue_command(s.get("command") or {})
        device_ids, action = [], None
        if command:
            target, action = command
            if target in v1_to_hub:
                device_ids = [v1_to_hub[target]]
        common = dict(
            id=f"huev1:{sid}",
            brand=self.brand,
            runs_on="Hue Bridge",
            origin="hub" if s.get("description") == HUB_MARK else "app",
            name=s.get("name") or "Hue schedule",
            device_ids=device_ids,
            device_names=[self._names.get(d, d) for d in device_ids],
            enabled=s.get("status") == "enabled",
        )
        if timing and action:
            time_, days = timing
            return Schedule(**common, action=action, trigger=Trigger(type="time", time=time_), days=days)
        return Schedule(**common, summary="Bridge schedule (timer or one-off)", can_toggle=True)

    def _behavior_to_schedule(self, b: dict) -> Schedule:
        trigger, days = None, []
        when = _find_key(b.get("configuration") or {}, "time_point")
        if isinstance(when, dict) and when.get("type") == "time":
            t = when.get("time") or {}
            if "hour" in t:
                trigger = Trigger(type="time", time=f"{t['hour']:02d}:{t.get('minute', 0):02d}")
        recurrence = _find_key(b.get("configuration") or {}, "recurrence_days")
        if isinstance(recurrence, list):
            days = [WEEKDAYS[d] for d in recurrence if d in WEEKDAYS]
        return Schedule(
            id=f"hueapp:{b['id']}",
            brand=self.brand,
            runs_on="Hue Bridge",
            origin="app",
            name=(b.get("metadata") or {}).get("name") or "Hue automation",
            trigger=trigger,
            days=days,
            enabled=bool(b.get("enabled")),
            can_delete=False,
            summary="Hue app automation",
        )

    async def create_schedule(self, spec: ScheduleIn, device_ids: list[str]) -> list[Schedule]:
        if not self._light_v1:
            await self.devices()
        created = []
        for hub_id in device_ids:
            v1 = self._light_v1.get(self._rid(hub_id))
            if v1 is None:
                raise HubError(404, f"Unknown Hue light {hub_id}")
            body = build_hue_schedule(
                self.config["app_key"], v1, spec.name, spec.action, spec.trigger, spec.days, spec.enabled
            )
            result = await self._v1("POST", "/schedules", body)
            sid = result[0]["success"]["id"]
            created.append(self._v1_to_schedule(sid, body, {v1: hub_id}))
        return created

    async def delete_schedule(self, schedule_id: str) -> None:
        kind, _, sid = schedule_id.partition(":")
        if kind != "huev1":
            raise HubError(400, "Hue app automations can only be deleted in the Hue app.")
        await self._v1("DELETE", f"/schedules/{sid}")

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        kind, _, sid = schedule_id.partition(":")
        if kind == "huev1":
            await self._v1("PUT", f"/schedules/{sid}", {"status": "enabled" if enabled else "disabled"})
        else:
            await self._put("behavior_instance", sid, {"enabled": enabled})

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _find_key(obj: Any, key: str) -> Any:
    """Depth-first search for a key in nested Hue automation configuration."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None
