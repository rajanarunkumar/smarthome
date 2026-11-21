"""LG webOS TV over its local WebSocket API (aiowebostv).

Pairing: Settings → LG TV → Connect, then accept the prompt on the TV with the
remote. The TV's key is saved in ~/.smarthome/secrets.json.
Turning the TV on uses Wake-on-LAN. On the TV, turn on Settings → General →
Devices → TV Management (or External Devices) → "Turn on via Wi-Fi".
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket

try:  # aiowebostv needs Python 3.12+; without it the hub runs with LG TV support off.
    import aiowebostv.webos_client as webos_client
    from aiowebostv import WebOsClient
    from aiowebostv.exceptions import WebOsTvError, WebOsTvPairError
except ImportError:  # pragma: no cover
    webos_client = WebOsClient = None

    class WebOsTvError(Exception):
        pass

    class WebOsTvPairError(WebOsTvError):
        pass

from ..config import Secrets
from ..models import Device, HubError, StateIn
from .base import Adapter

log = logging.getLogger(__name__)
SSDP_ADDR = ("239.255.255.250", 1900)
WEBOS_ST = "urn:lge-com:service:webos-second-screen:1"
LIVE_TV = "com.webos.app.livetv"
DEVICE_ID = "lgtv:tv"

# The library waits only 10 s for someone to accept the pairing prompt on the TV.
if webos_client is not None:
    webos_client.RECEIVE_TIMEOUT = 30


def magic_packet(mac: str) -> bytes:
    """Wake-on-LAN packet: 6 x 0xFF then the MAC address 16 times."""
    digits = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(digits) != 12:
        raise ValueError(f"Not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(digits) * 16


def send_wake(mac: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        packet = magic_packet(mac)
        for port in (9, 7):
            s.sendto(packet, ("255.255.255.255", port))


async def discover_tvs(timeout: float = 3.0) -> list[str]:
    """Find LG webOS TVs with SSDP; returns their IP addresses."""
    found: list[str] = []
    loop = asyncio.get_running_loop()

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple) -> None:
            if WEBOS_ST.encode() in data and addr[0] not in found:
                found.append(addr[0])

    transport, _ = await loop.create_datagram_endpoint(Proto, local_addr=("0.0.0.0", 0))
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {WEBOS_ST}\r\n\r\n').encode()
    try:
        for _ in range(2):
            transport.sendto(msg, SSDP_ADDR)
            await asyncio.sleep(timeout / 2)
    finally:
        transport.close()
    return found


def sources_of(state) -> list[dict[str, str]]:
    """Inputs (HDMI…) first, then apps, as [{"id", "label"}]."""
    inputs = [{"id": f"input:{i['id']}", "label": i.get("label") or i["id"]}
              for i in (state.inputs or {}).values()]
    apps = sorted(({"id": f"app:{a['id']}", "label": a.get("title") or a["id"]}
                   for a in (state.apps or {}).values() if not a.get("id", "").startswith("com.webos.app.hdmi")),
                  key=lambda x: x["label"].lower())
    return inputs + apps


def current_source(state) -> tuple[str | None, str | None]:
    """(source id, label) for what the TV is showing now."""
    app_id = state.current_app_id
    if not app_id:
        return None, None
    for i in (state.inputs or {}).values():
        if i.get("appId") == app_id:
            return f"input:{i['id']}", i.get("label")
    app = (state.apps or {}).get(app_id) or {}
    return f"app:{app_id}", app.get("title") or app_id


class LgTvAdapter(Adapter):
    brand = "lg"
    label = "LG TV"
    prefixes = ("lgtv",)
    can_rename = False

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._client: WebOsClient | None = None
        self._lock = asyncio.Lock()

    @property
    def config(self) -> dict:
        return self.secrets.get("lgtv") or {}

    @property
    def active(self) -> bool:
        return WebOsClient is not None and bool(self.config.get("client_key") and self.config.get("host"))

    async def _connect(self) -> WebOsClient | None:
        """Connected client, or None when the TV is off or unreachable."""
        async with self._lock:
            if self._client is not None and self._client.is_connected():
                return self._client
            if self._client is None:
                self._client = WebOsClient(self.config["host"], self.config["client_key"])
            try:
                await asyncio.wait_for(self._client.connect(), 6)
                return self._client
            except (OSError, asyncio.TimeoutError, WebOsTvError) as exc:
                log.debug("LG TV not reachable: %s", exc)
                return None

    async def pair(self, host: str | None = None) -> dict:
        if WebOsClient is None:
            raise HubError(501, "LG TV support needs Python 3.12 or newer. Recreate the hub's venv "
                                "with python3.12, then run pip install -r requirements.txt again.")
        if not host:
            hosts = await discover_tvs()
            if not hosts:
                raise HubError(404, "No LG TV found. Turn the TV on and make sure it's on the same Wi-Fi.")
            host = hosts[0]
        client = WebOsClient(host)  # no key: the TV shows an "Allow this device?" prompt
        try:
            await client.connect()
        except WebOsTvPairError as exc:
            raise HubError(403, "The TV said no, or nobody accepted the prompt in time. Try again.") from exc
        except (OSError, asyncio.TimeoutError, WebOsTvError) as exc:
            raise HubError(504, f"Couldn't reach the TV at {host}: {exc}") from exc
        try:
            info = await client.get_connection_info()
        except WebOsTvError:
            info = {}
        mac = next((v.get("macAddress") for k, v in info.items()
                    if isinstance(v, dict) and v.get("macAddress") and v.get("state", "connected") == "connected"),
                   None) or (info.get("wiredInfo") or {}).get("macAddress")
        model = client.tv_info.system.get("modelName")
        self.secrets.set("lgtv", {"host": host, "client_key": client.client_key, "mac": mac, "model": model})
        if self._client is not None:
            await self._client.disconnect()
        self._client = client
        return {"paired": True, "model": model, "wake_on_lan": bool(mac)}

    async def devices(self) -> list[Device]:
        client = await self._connect()
        base = dict(id=DEVICE_ID, brand=self.brand, name="TV", model=self.config.get("model"), kind="tv",
                    capabilities=["onoff", "volume", "mute", "source", "playback"])
        if client is None or not client.tv_state.is_on:
            # Off (or asleep): it still belongs in the list so it can be turned on.
            return [Device(**base, on=False, online=bool(self.config.get("mac")) or client is not None)]
        st = client.tv_state
        source, label = current_source(st)
        return [Device(**base, on=True, volume=st.volume, muted=st.muted, media=label,
                       source=source, sources=sources_of(st))]

    def names(self) -> dict[str, str]:
        return {DEVICE_ID: "TV"} if self.active else {}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        if device_id != DEVICE_ID:
            raise HubError(404, f"Unknown TV {device_id}")
        if state.on is True:
            client = await self._connect()
            if client is None or not client.tv_state.is_on:
                mac = self.config.get("mac")
                if not mac:
                    raise HubError(409, "The hub doesn't know the TV's network address to wake it. "
                                        "Turn the TV on with the remote once, then Connect again in Settings.")
                await asyncio.to_thread(send_wake, mac)
            return
        client = await self._connect()
        if client is None:
            raise HubError(503, "The TV is off or not reachable")
        try:
            if state.on is False:
                await client.power_off()
                return
            if state.volume is not None:
                await client.set_volume(state.volume)
            if state.muted is not None:
                await client.set_mute(state.muted)
            if state.source:
                kind, _, ident = state.source.partition(":")
                await (client.set_input(ident) if kind == "input" else client.launch_app(ident))
            if state.playback:
                live = client.tv_state.current_app_id == LIVE_TV
                action = {
                    "play": client.play, "pause": client.pause,
                    "next": client.channel_up if live else client.fast_forward,
                    "previous": client.channel_down if live else client.rewind,
                }[state.playback]
                await action()
        except WebOsTvError as exc:
            raise HubError(502, f"The TV didn't accept that: {exc}") from exc

    async def status(self) -> dict:
        return {"paired": self.active, "available": WebOsClient is not None, "model": self.config.get("model"),
                "wake_on_lan": bool(self.config.get("mac"))}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
