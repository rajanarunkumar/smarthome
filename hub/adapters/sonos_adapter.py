"""Sonos speakers over the local UPnP API (soco). Alarms are stored and run on the speakers."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

import soco
from soco import alarms as soco_alarms

from ..config import Secrets
from ..models import DAYS, Action, Device, HubError, Schedule, ScheduleIn, StateIn, Trigger

log = logging.getLogger(__name__)
REDISCOVER_SECONDS = 300
DEFAULT_ALARM_VOLUME = 20
ALARM_DURATION = "01:00:00"  # Same default as the Sonos app
# Sonos recurrence digits run Sunday=0 .. Saturday=6.
_SONOS_DIGIT = {"sun": "0", "mon": "1", "tue": "2", "wed": "3", "thu": "4", "fri": "5", "sat": "6"}
_KEYWORDS = soco_alarms.RECURRENCE_KEYWORD_EQUIVALENT


def days_to_recurrence(days: list[str]) -> str:
    return "ON_" + "".join(sorted(_SONOS_DIGIT[d] for d in set(days)))


def recurrence_to_days(recurrence: str) -> list[str]:
    rec = _KEYWORDS.get(recurrence, recurrence)
    digits = set(rec[3:]) if rec.startswith("ON_") else set()
    return [d for d in DAYS if _SONOS_DIGIT[d] in digits]


class SonosAdapter:
    brand = "sonos"

    def __init__(self, secrets: Secrets) -> None:
        self._secrets = secrets
        self._zones: dict[str, soco.SoCo] = {}
        self._online: dict[str, bool] = {}
        self._names: dict[str, str] = {}  # last known names, so names() never touches the network
        self._last_discovery = 0.0
        self._discovering = asyncio.Lock()

    # Discovery
    async def discover(self) -> None:
        async with self._discovering:
            found = await asyncio.to_thread(soco.discover, timeout=4) or set()
            for zone in found:
                self._zones["sonos:" + zone.uid.lower()] = zone
            self._last_discovery = time.monotonic()

    async def _ensure(self) -> None:
        if not self._zones or time.monotonic() - self._last_discovery > REDISCOVER_SECONDS:
            await self.discover()

    def _get(self, did: str) -> soco.SoCo:
        zone = self._zones.get(did)
        if zone is None:
            raise HubError(404, f"Unknown Sonos speaker {did}")
        return zone

    @staticmethod
    def _snapshot(zone: soco.SoCo) -> dict:
        """Blocking: reads one speaker's state (runs in a worker thread)."""
        info = zone.get_speaker_info()
        playing = zone.group.coordinator.get_current_transport_info()["current_transport_state"]
        return {
            "name": zone.player_name,
            "model": info.get("model_name"),
            "volume": zone.volume,
            "playing": playing in {"PLAYING", "TRANSITIONING"},
        }

    async def _to_device(self, did: str, zone: soco.SoCo) -> Device:
        try:
            snap = await asyncio.to_thread(self._snapshot, zone)
            self._online[did] = True
            self._names[did] = snap["name"]
        except Exception as exc:  # noqa: BLE001 - one bad speaker must not stop the rest
            log.warning("Sonos %s did not answer: %s", did, exc)
            self._online[did] = False
            return Device(id=did, brand=self.brand, name=self.names().get(did, "Sonos"),
                          kind="speaker", online=False, capabilities=["play", "volume"])
        return Device(
            id=did, brand=self.brand, name=snap["name"], model=snap["model"], kind="speaker",
            on=snap["playing"], volume=snap["volume"], capabilities=["play", "volume", "favorites"],
        )

    async def devices(self) -> list[Device]:
        await self._ensure()
        return list(await asyncio.gather(*(self._to_device(d, z) for d, z in self._zones.items())))

    def names(self) -> dict[str, str]:
        return dict(self._names)

    async def favorites(self, did: str) -> list[str]:
        zone = self._get(did)
        favs = await asyncio.to_thread(zone.music_library.get_sonos_favorites)
        return [f.title for f in favs]

    async def set_state(self, did: str, state: StateIn) -> None:
        zone = self._get(did)

        def apply() -> None:
            coordinator = zone.group.coordinator
            if state.volume is not None:
                zone.volume = state.volume
            if state.favorite:
                fav = next((f for f in zone.music_library.get_sonos_favorites() if f.title == state.favorite), None)
                if fav is None:
                    raise HubError(404, f"No Sonos favorite called {state.favorite}")
                coordinator.play_uri(fav.resource.uri, fav.resource_meta_data, title=fav.title)
            elif state.on is True:
                coordinator.play()
            elif state.on is False:
                coordinator.pause()

        try:
            await asyncio.to_thread(apply)
        except HubError:
            raise
        except soco.exceptions.SoCoUPnPException as exc:
            if state.on is True:
                raise HubError(409, "Nothing to play. Pick a Sonos favorite first.") from exc
            raise HubError(502, f"Sonos refused: {exc}") from exc

    async def rename(self, did: str, name: str) -> None:
        zone = self._get(did)

        def apply() -> None:
            zone.player_name = name

        await asyncio.to_thread(apply)
        self._names[did] = name

    # Schedules: Sonos alarms, stored on the speakers
    def _hub_alarms(self) -> dict[str, str]:
        """Alarm IDs this hub created, with the names given here (Sonos alarms have no name)."""
        return self._secrets.get("sonos_alarms") or {}

    def _alarm_to_schedule(self, alarm: soco_alarms.Alarm) -> Schedule:
        did = "sonos:" + alarm.zone.uid.lower()
        hub_name = self._hub_alarms().get(str(alarm.alarm_id))
        start: dt.time = alarm.start_time
        return Schedule(
            id=f"sonosalarm:{alarm.alarm_id}",
            brand=self.brand,
            runs_on="Sonos speaker",
            origin="hub" if hub_name else "app",
            name=hub_name or "Sonos alarm",
            device_ids=[did],
            action=Action(on=True, volume=alarm.volume),
            trigger=Trigger(type="time", time=start.strftime("%H:%M")),
            days=recurrence_to_days(alarm.recurrence),
            enabled=alarm.enabled,
        )

    async def _alarms(self) -> dict[str, soco_alarms.Alarm]:
        await self._ensure()
        if not self._zones:
            return {}
        any_zone = next(iter(self._zones.values()))
        found = await asyncio.to_thread(soco_alarms.get_alarms, any_zone)
        return {str(a.alarm_id): a for a in found}

    async def schedules(self) -> list[Schedule]:
        return [self._alarm_to_schedule(a) for a in (await self._alarms()).values()]

    async def create_schedule(self, spec: ScheduleIn, device_ids: list[str]) -> list[Schedule]:
        if not spec.action.on:
            raise HubError(400, "Sonos alarms can only start playing. They stop by themselves after an hour.")
        if spec.trigger.type != "time":
            raise HubError(400, "Sonos alarms need a set time, not sunrise or sunset.")
        volume = spec.action.volume or spec.action.brightness or DEFAULT_ALARM_VOLUME
        start = dt.datetime.strptime(spec.trigger.time, "%H:%M").time()
        await self._ensure()
        created: list[Schedule] = []
        for did in device_ids:
            zone = self._get(did)
            alarm = soco_alarms.Alarm(
                zone, start_time=start, duration=dt.datetime.strptime(ALARM_DURATION, "%H:%M:%S").time(),
                recurrence=days_to_recurrence(spec.days), enabled=spec.enabled, volume=volume,
            )
            await asyncio.to_thread(alarm.save)
            self._secrets.set("sonos_alarms", {**self._hub_alarms(), str(alarm.alarm_id): spec.name})
            created.append(self._alarm_to_schedule(alarm))
        return created

    async def _alarm(self, schedule_id: str) -> soco_alarms.Alarm:
        alarm = (await self._alarms()).get(schedule_id.split(":", 1)[1])
        if alarm is None:
            raise HubError(404, "Schedule not found")
        return alarm

    async def delete_schedule(self, schedule_id: str) -> None:
        alarm = await self._alarm(schedule_id)
        alarm_id = str(alarm.alarm_id)  # remove() clears alarm_id
        await asyncio.to_thread(alarm.remove)
        mine = self._hub_alarms()
        if mine.pop(alarm_id, None) is not None:
            self._secrets.set("sonos_alarms", mine)

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        alarm = await self._alarm(schedule_id)
        alarm.enabled = enabled
        await asyncio.to_thread(alarm.save)

    async def close(self) -> None:
        return None
