"""Govee lights and plugs.

Local first: the Govee LAN API (UDP) for lights with "LAN Control" turned on in
the Govee app. Optional Govee account (API key, see govee_cloud.py) for
everything else, and for the device names from the Govee app. A light that
answers on the LAN is always controlled locally.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time

from ..color import mirek_to_hex
from ..config import Secrets
from ..models import Device, HubError, StateIn
from . import govee_cloud as cloud_api
from . import govee_codec as codec
from .base import Adapter

log = logging.getLogger(__name__)
REDISCOVER_SECONDS = 120
OFFLINE_AFTER_SECONDS = 15


class _Replies(asyncio.DatagramProtocol):
    def __init__(self, adapter: GoveeAdapter) -> None:
        self.adapter = adapter

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.adapter._on_reply(addr[0], data)


class GoveeAdapter(Adapter):
    brand = "govee"
    label = "Govee"
    prefixes = ("govee",)
    can_rename = False

    def __init__(self, secrets: Secrets | None = None) -> None:
        self.secrets = secrets
        key = secrets.get("govee_api_key") if secrets else None
        self.cloud: cloud_api.GoveeCloud | None = cloud_api.GoveeCloud(key) if key else None
        self._lights: dict[str, dict] = {}       # hub id -> {ip, sku, state, seen}
        self._by_ip: dict[str, str] = {}
        self._status_waiters: dict[str, asyncio.Future] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._last_scan = float("-inf")  # never

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", codec.REPLY_PORT))
        except OSError as exc:
            log.warning("Govee disabled: UDP port %s is in use (%s)", codec.REPLY_PORT, exc)
            sock.close()
            return
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 2))
        sock.setblocking(False)
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _Replies(self), sock=sock)
        await self.discover()

    # Replies from lights (scan answers and status answers both arrive on 4002)
    def _on_reply(self, ip: str, data: bytes) -> None:
        parsed = codec.parse(data)
        if parsed is None:
            return
        cmd, body = parsed
        if cmd == "scan" and body.get("device"):
            hub_id = "govee:" + body["device"].replace(":", "").lower()
            light = self._lights.setdefault(hub_id, {"state": {}})
            light.update(ip=body.get("ip") or ip, sku=body.get("sku"), seen=time.monotonic())
            self._by_ip[light["ip"]] = hub_id
        elif cmd == "devStatus":
            hub_id = self._by_ip.get(ip)
            if hub_id is None:
                return
            self._lights[hub_id]["state"] = codec.status_to_state(body)
            self._lights[hub_id]["seen"] = time.monotonic()
            waiter = self._status_waiters.pop(hub_id, None)
            if waiter and not waiter.done():
                waiter.set_result(True)

    def _send(self, payload: bytes, addr: tuple) -> None:
        if self._transport is None:
            raise HubError(503, "Govee isn't available: the hub couldn't open its UDP port")
        self._transport.sendto(payload, addr)

    async def discover(self) -> None:
        if self._transport is None:
            return
        self._send(codec.scan(), codec.SCAN_GROUP)
        self._last_scan = time.monotonic()
        await asyncio.sleep(2)

    async def _refresh(self, hub_id: str) -> None:
        loop = asyncio.get_running_loop()
        waiter = self._status_waiters[hub_id] = loop.create_future()
        self._send(codec.status_request(), (self._lights[hub_id]["ip"], codec.COMMAND_PORT))
        try:
            await asyncio.wait_for(waiter, 2)
        except asyncio.TimeoutError:
            self._status_waiters.pop(hub_id, None)

    def _on_lan(self, hub_id: str) -> bool:
        light = self._lights.get(hub_id)
        return bool(light) and time.monotonic() - light.get("seen", 0) < OFFLINE_AFTER_SECONDS

    async def devices(self) -> list[Device]:
        if self._transport is not None:
            if time.monotonic() - self._last_scan > REDISCOVER_SECONDS:
                await self.discover()
            await asyncio.gather(*(self._refresh(h) for h in list(self._lights)))
        cloud_devices: dict[str, dict] = {}
        if self.cloud is not None:
            cloud_devices = await self.cloud.list_devices()
            stale = [h for h in cloud_devices if not self._on_lan(h)]
            results = await asyncio.gather(*(self.cloud.refresh(h) for h in stale), return_exceptions=True)
            for h, r in zip(stale, results):
                if isinstance(r, BaseException):
                    log.warning("Govee cloud state for %s failed: %s", h, r)
        names = self.names()
        out = []
        for hub_id in sorted(set(self._lights) | set(cloud_devices)):
            record = cloud_devices.get(hub_id, {})
            local = self._on_lan(hub_id)
            if local:
                st, online = self._lights[hub_id]["state"], True
            else:
                st = self.cloud.state.get(hub_id, {}) if self.cloud and record else {}
                online = st.get("online", bool(record))
            kelvin = cloud_api.kelvin_range(record) or (codec.KELVIN_MIN, codec.KELVIN_MAX)
            if record:
                have = cloud_api.caps_of(record)
                caps = [c for c, k in (("onoff", cloud_api.ON_OFF), ("brightness", cloud_api.BRIGHTNESS),
                                       ("color", cloud_api.RGB), ("color_temp", cloud_api.KELVIN)) if k in have]
                kind = cloud_api.KINDS.get(record.get("type"), "light")
            else:
                caps, kind = ["onoff", "brightness", "color", "color_temp"], "light"
            color = st.get("color") or (mirek_to_hex(st["mirek"]) if st.get("mirek") else None)
            out.append(Device(
                id=hub_id, brand=self.brand, name=names[hub_id],
                model=record.get("sku") or self._lights.get(hub_id, {}).get("sku"),
                kind=kind, online=online, on=st.get("on"),
                brightness=st.get("brightness") if "brightness" in caps else None,
                color=color if kind == "light" else None, mirek=st.get("mirek"),
                mirek_min=codec.kelvin_to_mirek(kelvin[1]), mirek_max=codec.kelvin_to_mirek(kelvin[0]),
                capabilities=caps,
            ))
        return out

    def names(self) -> dict[str, str]:
        names = {h: f"Govee {l.get('sku') or 'light'}" for h, l in self._lights.items()}
        if self.cloud is not None:
            names.update({h: d.get("deviceName") or f"Govee {d.get('sku')}" for h, d in self.cloud.devices.items()})
        return names

    # Govee account
    async def connect_account(self, api_key: str) -> dict:
        candidate = cloud_api.GoveeCloud(api_key.strip())
        try:
            found = await candidate.list_devices(force=True)
        except HubError:
            await candidate.close()
            raise
        if self.cloud is not None:
            await self.cloud.close()
        self.cloud = candidate
        self.secrets.set("govee_api_key", api_key.strip())
        return {"connected": True, "devices": len(found)}

    async def disconnect_account(self) -> None:
        if self.cloud is not None:
            await self.cloud.close()
        self.cloud = None
        self.secrets.set("govee_api_key", None)

    async def set_state(self, device_id: str, state: StateIn) -> None:
        if not self._on_lan(device_id) and self.cloud is not None and device_id in self.cloud.devices:
            await self._cloud_set_state(device_id, state)
            return
        light = self._lights.get(device_id)
        if light is None:
            raise HubError(404, f"Unknown Govee light {device_id}")
        addr = (light["ip"], codec.COMMAND_PORT)
        if state.on is not None:
            self._send(codec.turn(state.on), addr)
        if state.brightness is not None:
            self._send(codec.brightness(state.brightness), addr)
        if state.color is not None:
            self._send(codec.color(state.color), addr)
        if state.mirek is not None:
            self._send(codec.color_temp(state.mirek), addr)
        light["state"].update({k: v for k, v in state.model_dump().items()
                               if k in ("on", "brightness", "color", "mirek") and v is not None})

    async def _cloud_set_state(self, device_id: str, state: StateIn) -> None:
        cloud = self.cloud
        record = cloud.devices[device_id]
        cached = cloud.state.setdefault(device_id, {})
        if state.on is not None:
            await cloud.control(device_id, cloud_api.ON_OFF, 1 if state.on else 0)
            cached["on"] = state.on
        if state.brightness is not None:
            await cloud.control(device_id, cloud_api.BRIGHTNESS, state.brightness)
            cached["brightness"] = state.brightness
        if state.color is not None:
            await cloud.control(device_id, cloud_api.RGB, cloud_api.rgb_to_int(state.color))
            cached.update(color=state.color, mirek=None)
        if state.mirek is not None:
            low, high = cloud_api.kelvin_range(record) or (codec.KELVIN_MIN, codec.KELVIN_MAX)
            await cloud.control(device_id, cloud_api.KELVIN, max(low, min(high, round(1_000_000 / state.mirek))))
            cached.update(mirek=state.mirek, color=None)

    async def status(self) -> dict:
        lan = sum(1 for h in self._lights if self._on_lan(h))
        cloud_only = len(set(self.cloud.devices) - set(self._lights)) if self.cloud else 0
        return {"devices": lan + cloud_only, "lan": lan, "listening": self._transport is not None,
                "account": self.cloud is not None}

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
        if self.cloud is not None:
            await self.cloud.close()
