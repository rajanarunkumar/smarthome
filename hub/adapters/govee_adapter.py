"""Govee lights over the Govee LAN API (local UDP, no cloud, no login).

Turn on "LAN Control" for each light in the Govee app (device settings) first.
The LAN API has no names, so names are kept in the hub.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time

from ..color import mirek_to_hex
from ..models import Device, HubError, StateIn
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

    def __init__(self) -> None:
        self._lights: dict[str, dict] = {}       # hub id -> {ip, sku, state, seen}
        self._by_ip: dict[str, str] = {}
        self._status_waiters: dict[str, asyncio.Future] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._last_scan = 0.0

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

    async def devices(self) -> list[Device]:
        if self._transport is None:
            return []
        if time.monotonic() - self._last_scan > REDISCOVER_SECONDS:
            await self.discover()
        await asyncio.gather(*(self._refresh(h) for h in list(self._lights)))
        now = time.monotonic()
        out = []
        for hub_id, light in self._lights.items():
            st = light["state"]
            color = st.get("color") or (mirek_to_hex(st["mirek"]) if st.get("mirek") else None)
            out.append(Device(
                id=hub_id, brand=self.brand, name=self.names()[hub_id], model=light.get("sku"),
                kind="light", online=now - light.get("seen", 0) < OFFLINE_AFTER_SECONDS,
                on=st.get("on"), brightness=st.get("brightness"), color=color, mirek=st.get("mirek"),
                mirek_min=codec.kelvin_to_mirek(codec.KELVIN_MAX),
                mirek_max=codec.kelvin_to_mirek(codec.KELVIN_MIN),
                capabilities=["onoff", "brightness", "color", "color_temp"],
            ))
        return out

    def names(self) -> dict[str, str]:
        return {h: f"Govee {l.get('sku') or 'light'}" for h, l in self._lights.items()}

    async def set_state(self, device_id: str, state: StateIn) -> None:
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

    async def status(self) -> dict:
        return {"devices": len(self._lights), "listening": self._transport is not None}

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
