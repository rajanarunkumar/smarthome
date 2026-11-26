"""TP-Link Kasa plugs and dimmers over the local protocol (no cloud, no login)."""

from __future__ import annotations

import asyncio
import logging
import time

from kasa import Device as KasaDevice
from kasa import Discover, Module

from ..models import Device, HubError, Schedule, ScheduleIn, StateIn
from ..schedule_codec import build_kasa_rule, parse_kasa_rule
from .base import Adapter

log = logging.getLogger(__name__)
REDISCOVER_SECONDS = 300


class KasaAdapter(Adapter):
    brand = "kasa"
    label = "Kasa"
    prefixes = ("kasa",)

    def __init__(self) -> None:
        self._devs: dict[str, KasaDevice] = {}
        self._online: dict[str, bool] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_discovery = 0.0
        self._discovering = asyncio.Lock()

    async def start(self) -> None:
        asyncio.create_task(self.discover())

    async def status(self) -> dict:
        return {"devices": len(self._devs)}

    # Discovery and state
    async def discover(self) -> None:
        async with self._discovering:
            found = await Discover.discover(timeout=4)
            for dev in found.values():
                try:
                    await dev.update()
                except Exception as exc:  # noqa: BLE001 - one bad device must not stop the rest
                    log.warning("Kasa device at %s did not answer: %s", dev.host, exc)
                    continue
                did = "kasa:" + dev.mac.replace(":", "").lower()
                old = self._devs.get(did)
                self._devs[did] = dev
                self._online[did] = True
                if old is not None and old is not dev:
                    await old.disconnect()
            self._last_discovery = time.monotonic()

    async def _ensure(self) -> None:
        if not self._devs or time.monotonic() - self._last_discovery > REDISCOVER_SECONDS:
            await self.discover()

    def _lock(self, did: str) -> asyncio.Lock:
        return self._locks.setdefault(did, asyncio.Lock())

    def _get(self, did: str) -> KasaDevice:
        dev = self._devs.get(did)
        if dev is None:
            raise HubError(404, f"Unknown Kasa device {did}")
        return dev

    async def _refresh(self, did: str) -> None:
        try:
            async with self._lock(did):
                await self._devs[did].update()
            self._online[did] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("Kasa %s update failed: %s", did, exc)
            self._online[did] = False

    @staticmethod
    def _is_dimmer(dev: KasaDevice) -> bool:
        return Module.Light in dev.modules

    def _to_device(self, did: str, dev: KasaDevice) -> Device:
        dimmer = self._is_dimmer(dev)
        energy = dev.modules.get(Module.Energy)     # only plugs with energy monitoring (KP115, KP125, EP25, HS110…)
        watts = details = None
        if energy is not None:
            try:
                watts = round(float(energy.current_consumption), 1) if energy.current_consumption is not None else None
                today = energy.consumption_today
                details = [x for x in ({"label": "Power now", "value": f"{watts:g} W"} if watts is not None else None,
                                       {"label": "Today", "value": f"{today:.2f} kWh"} if today is not None else None) if x]
            except Exception:  # noqa: BLE001 - a plug that hasn't reported yet
                watts = None
        return Device(
            id=did,
            brand=self.brand,
            name=dev.alias or dev.model,
            model=dev.model,
            kind="dimmer" if dimmer else "plug",
            online=self._online.get(did, True),
            on=dev.is_on,
            brightness=dev.modules[Module.Light].brightness if dimmer else None,
            watts=watts, details=details or [],
            capabilities=["onoff", "brightness", "schedule"] if dimmer else ["onoff", "schedule"],
        )

    async def devices(self) -> list[Device]:
        await self._ensure()
        await asyncio.gather(*(self._refresh(did) for did in list(self._devs)))
        return [self._to_device(did, dev) for did, dev in self._devs.items()]

    def names(self) -> dict[str, str]:
        return {did: (dev.alias or dev.model) for did, dev in self._devs.items()}

    async def set_state(self, did: str, state: StateIn) -> None:
        dev = self._get(did)
        async with self._lock(did):
            if state.brightness is not None and self._is_dimmer(dev):
                await dev.modules[Module.Light].set_brightness(state.brightness)
                if state.on is not False:
                    await dev.turn_on()
            if state.on is True:
                await dev.turn_on()
            elif state.on is False:
                await dev.turn_off()
            await dev.update()

    async def rename(self, did: str, name: str) -> None:
        dev = self._get(did)
        async with self._lock(did):
            await dev.set_alias(name)
            await dev.update()

    # Schedules (stored and run on the plug itself)
    @staticmethod
    def _raw_rules(dev: KasaDevice) -> list[dict]:
        module = dev.modules.get("schedule")
        if module is None:
            return []
        return (module.data.get("get_rules") or {}).get("rule_list", [])

    async def schedules(self) -> list[Schedule]:
        await self._ensure()
        out: list[Schedule] = []
        for did, dev in self._devs.items():
            for rule in self._raw_rules(dev):
                out.append(self._rule_to_schedule(did, dev, rule))
        return out

    def _rule_to_schedule(self, did: str, dev: KasaDevice, rule: dict) -> Schedule:
        parsed = parse_kasa_rule(rule)
        name = rule.get("name") or "Schedule"
        common = dict(
            id=f"{did}:{rule['id']}",
            brand=self.brand,
            runs_on="Kasa plug",
            # The Kasa app names every rule "name"; anything else came from the hub.
            origin="app" if name == "name" else "hub",
            name="Kasa app schedule" if name == "name" else name,
            device_ids=[did],
            device_names=[dev.alias or dev.model],
            enabled=bool(rule.get("enable", 1)),
        )
        if parsed is None:
            return Schedule(**common, summary="Kasa schedule in a format the hub can't show yet")
        action, trigger, days = parsed
        return Schedule(**common, action=action, trigger=trigger, days=days)

    def _sun_estimate(self, stime_opt: int) -> int | None:
        for dev in self._devs.values():
            for rule in self._raw_rules(dev):
                if rule.get("stime_opt") == stime_opt and not rule.get("soffset"):
                    return int(rule.get("smin", 0))
        return None

    async def create_schedule(self, spec: ScheduleIn, device_ids: list[str]) -> list[Schedule]:
        await self._ensure()
        created: list[Schedule] = []
        stime_opt = {"time": 0, "sunrise": 1, "sunset": 2}[spec.trigger.type]
        for did in device_ids:
            dev = self._get(did)
            rule = build_kasa_rule(
                spec.name, spec.action, spec.trigger, spec.days, spec.enabled,
                is_dimmer=self._is_dimmer(dev), sun_smin=self._sun_estimate(stime_opt),
            )
            async with self._lock(did):
                result = await dev.modules["schedule"].call("add_rule", rule)
                await dev.update()
            rule_id = (result or {}).get("id")
            match = next((r for r in self._raw_rules(dev) if r.get("id") == rule_id), None)
            if match is None:
                raise HubError(502, f"{dev.alias} did not save the schedule")
            created.append(self._rule_to_schedule(did, dev, match))
        return created

    def _split(self, schedule_id: str) -> tuple[str, KasaDevice, str]:
        did, _, rule_id = schedule_id.rpartition(":")
        return did, self._get(did), rule_id

    async def delete_schedule(self, schedule_id: str) -> None:
        did, dev, rule_id = self._split(schedule_id)
        async with self._lock(did):
            await dev.modules["schedule"].call("delete_rule", {"id": rule_id})
            await dev.update()

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        did, dev, rule_id = self._split(schedule_id)
        rule = next((r for r in self._raw_rules(dev) if r.get("id") == rule_id), None)
        if rule is None:
            raise HubError(404, "Schedule not found")
        async with self._lock(did):
            # Stored rules omit the end-time fields, but edit_rule rejects a rule without them.
            edited = {"etime_opt": -1, "emin": 0, **rule, "enable": int(enabled)}
            await dev.modules["schedule"].call("edit_rule", edited)
            await dev.update()

    async def close(self) -> None:
        for dev in self._devs.values():
            await dev.disconnect()
