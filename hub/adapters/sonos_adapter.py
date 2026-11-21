"""Sonos speakers over their local UPnP API (soco). No Sonos account needed.

Play/pause/next/previous go to the speaker's group coordinator, so grouped
speakers act together, as in the Sonos app. Volume and mute are per speaker.
"""

from __future__ import annotations

import asyncio
import logging
import time

import soco
from soco.exceptions import SoCoUPnPException

from ..models import Device, HubError, StateIn
from .base import Adapter

log = logging.getLogger(__name__)
REDISCOVER_SECONDS = 300


def snapshot(player) -> dict:
    """Read one speaker's state (blocking: run in a thread)."""
    coordinator = player.group.coordinator if player.group else player
    transport = coordinator.get_current_transport_info().get("current_transport_state")
    if player.is_playing_tv:
        media = "TV"
    else:
        track = coordinator.get_current_track_info()
        media = " – ".join(x for x in (track.get("title"), track.get("artist")) if x) or None
    return {
        "name": player.player_name,
        "model": player.get_speaker_info().get("model_name"),
        "volume": player.volume,
        "muted": player.mute,
        "playing": transport == "PLAYING",
        "media": media,
    }


class SonosAdapter(Adapter):
    brand = "sonos"
    label = "Sonos"
    prefixes = ("sonos",)

    def __init__(self) -> None:
        self._players: dict[str, object] = {}
        self._names: dict[str, str] = {}
        self._last_discovery = 0.0
        self._discovering = asyncio.Lock()

    async def start(self) -> None:
        asyncio.create_task(self.discover())

    async def discover(self) -> None:
        async with self._discovering:
            found = await asyncio.to_thread(soco.discover, timeout=4) or set()
            for p in found:
                self._players["sonos:" + p.uid.lower()] = p
            self._last_discovery = time.monotonic()

    def _get(self, device_id: str):
        player = self._players.get(device_id)
        if player is None:
            raise HubError(404, f"Unknown Sonos speaker {device_id}")
        return player

    async def _device(self, device_id: str, player) -> Device:
        try:
            s = await asyncio.to_thread(snapshot, player)
        except Exception as exc:  # noqa: BLE001 - one bad speaker must not stop the rest
            log.warning("Sonos %s did not answer: %s", device_id, exc)
            return Device(id=device_id, brand=self.brand, kind="speaker", online=False,
                          name=self._names.get(device_id, "Sonos speaker"))
        self._names[device_id] = s["name"]
        return Device(
            id=device_id, brand=self.brand, name=s["name"], model=s["model"], kind="speaker",
            on=s["playing"], playing=s["playing"], volume=s["volume"], muted=s["muted"], media=s["media"],
            capabilities=["onoff", "playback", "volume", "mute"],
        )

    async def devices(self) -> list[Device]:
        if not self._players or time.monotonic() - self._last_discovery > REDISCOVER_SECONDS:
            await self.discover()
        return list(await asyncio.gather(*(self._device(d, p) for d, p in self._players.items())))

    def names(self) -> dict[str, str]:
        return dict(self._names)

    async def set_state(self, device_id: str, state: StateIn) -> None:
        player = self._get(device_id)

        def apply() -> None:
            coordinator = player.group.coordinator if player.group else player
            # For a speaker, the power switch means play / pause.
            playback = state.playback or ({True: "play", False: "pause"}.get(state.on))
            if state.volume is not None:
                player.volume = state.volume
            if state.muted is not None:
                player.mute = state.muted
            if playback:
                getattr(coordinator, playback)()

        try:
            await asyncio.to_thread(apply)
        except SoCoUPnPException as exc:
            # e.g. "next" on a radio station, or "play" with nothing queued
            raise HubError(409, f"Sonos said no: {exc.error_description or exc}") from exc

    async def rename(self, device_id: str, name: str) -> None:
        player = self._get(device_id)
        await asyncio.to_thread(setattr, player, "player_name", name)
        self._names[device_id] = name

    async def status(self) -> dict:
        return {"devices": len(self._players)}
