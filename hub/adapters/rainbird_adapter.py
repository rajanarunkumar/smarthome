"""Rain Bird irrigation controllers with a LNK/LNK2 Wi-Fi module, controlled
locally (pyrainbird, the library Home Assistant's Rain Bird integration uses).
No Rain Bird account: the hub talks to the controller on your Wi-Fi with the
controller password set in the Rain Bird app.

What the hub does: run a zone for N minutes, stop watering, set a rain delay,
show what's watering and time left, the rain sensor, and the controller's own
programs (read only). Zone names aren't stored on the controller (the Rain Bird
app keeps them in its cloud), so names you give zones are kept in the hub.

pyrainbird needs Python 3.13+. Without it the hub runs with Rain Bird off.
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
STATE_SECONDS = 15
SCHEDULE_SECONDS = 3600
DEVICE_ID = "rainbird:controller"

try:
    import aiohttp
    from pyrainbird.async_client import create_controller
    from pyrainbird.exceptions import RainbirdApiException, RainbirdAuthException
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

    class RainbirdApiException(Exception):
        pass

    class RainbirdAuthException(RainbirdApiException):
        pass

DAY_ABBR = {"MONDAY": "Mon", "TUESDAY": "Tue", "WEDNESDAY": "Wed", "THURSDAY": "Thu", "FRIDAY": "Fri",
            "SATURDAY": "Sat", "SUNDAY": "Sun"}


def describe_program(program: Any) -> dict[str, str] | None:
    """{"label": "Program A", "value": "5:00 AM, 6:30 PM · Mon Wed Fri · 45 min"} or None if it never runs."""
    starts = [s for s in (program.starts or []) if s is not None]
    total = sum(int(d.duration.total_seconds() // 60) for d in (program.durations or []))
    freq = getattr(program.frequency, "name", str(program.frequency))
    if not starts or not total or freq == "OFF":
        return None
    if freq == "CUSTOM":
        order = list(DAY_ABBR)
        days = " ".join(DAY_ABBR[d.name] for d in sorted(program.days_of_week or [], key=lambda d: order.index(d.name)))
    elif freq == "CYCLIC":
        days = f"Every {program.period} days" if program.period else "Cyclic"
    else:
        days = {"ODD": "Odd days", "EVEN": "Even days"}.get(freq, freq.title())
    times = ", ".join(s.strftime("%-I:%M %p") for s in starts)
    return {"label": f"Program {program.name}", "value": f"{times} · {days or 'No days'} · {total} min"}


def status_text(state: Any, zone_label) -> str:
    if state.irrigation_state and state.active_station:
        left = f" · {max(1, round(state.remaining_runtime / 60))} min left" if state.remaining_runtime else ""
        return f"Watering {zone_label(state.active_station)}{left}"
    bits = ["Idle"]
    if state.delay_setting:
        bits.append(f"Rain delay {state.delay_setting} day{'s' if state.delay_setting != 1 else ''}")
    if state.sensor_state:
        bits.append("Rain sensor wet")
    return " · ".join(bits)


class RainBirdAdapter(Adapter):
    brand = "rainbird"
    label = "Rain Bird"
    prefixes = ("rainbird",)
    can_rename = False

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._session: Any = None
        self._ctl: Any = None
        self._model: str | None = None
        self._zones: list[int] = []
        self._state: Any = None
        self._state_at = float("-inf")  # never
        self._programs: list[dict[str, str]] = []
        self._programs_at = float("-inf")
        self._lock = asyncio.Lock()

    @property
    def config(self) -> dict:
        return self.secrets.get("rainbird") or {}

    @property
    def active(self) -> bool:
        return AVAILABLE and bool(self.config.get("host"))

    def zone_label(self, zone: int) -> str:
        name = (self.config.get("zone_names") or {}).get(str(zone))
        return f"Zone {zone}" + (f" ({name})" if name else "")

    async def _controller(self, cfg: dict | None = None) -> Any:
        cfg = cfg or self.config
        if self._ctl is None:
            self._session = aiohttp.ClientSession()
            self._ctl = await create_controller(self._session, cfg["host"], cfg["password"])
            info = await self._ctl.get_model_and_version()
            self._model = info.model_name
            self._zones = sorted((await self._ctl.get_available_stations()).active_set)
        return self._ctl

    async def connect(self, host: str, password: str) -> dict:
        if not AVAILABLE:
            raise HubError(501, "Rain Bird support needs Python 3.13 or newer on the hub Mac. See the README.")
        cfg = {"host": host.strip(), "password": password, "zone_names": self.config.get("zone_names") or {}}
        await self.close()
        try:
            await self._controller(cfg)
        except RainbirdAuthException as exc:
            await self.close()
            raise HubError(401, "The controller didn't accept that password. It's the controller password "
                                "set in the Rain Bird app, not your account password.") from exc
        except (RainbirdApiException, OSError, asyncio.TimeoutError) as exc:
            await self.close()
            raise HubError(504, f"Couldn't reach a Rain Bird controller at {host}: {exc}") from exc
        self.secrets.set("rainbird", cfg)
        return {"connected": True, "devices": 1, "zones": len(self._zones), "model": self._model}

    async def disconnect(self) -> None:
        await self.close()
        self.secrets.set("rainbird", None)

    def name_zone(self, zone: int, name: str) -> None:
        cfg = dict(self.config)
        names = dict(cfg.get("zone_names") or {})
        if name.strip():
            names[str(zone)] = name.strip()[:24]
        else:
            names.pop(str(zone), None)
        self.secrets.set("rainbird", {**cfg, "zone_names": names})

    async def _refresh(self) -> None:
        async with self._lock:
            ctl = await self._controller()
            now = time.monotonic()
            if now - self._state_at > STATE_SECONDS:
                self._state = await ctl.get_combined_controller_state()
                self._state_at = now
            if now - self._programs_at > SCHEDULE_SECONDS:
                try:
                    schedule = await ctl.get_schedule()
                    self._programs = [p for p in (describe_program(x) for x in schedule.programs) if p]
                except Exception as exc:  # noqa: BLE001 - older controllers can't report programs
                    log.info("Rain Bird programs unavailable: %s", exc)
                    self._programs = []
                self._programs_at = now

    async def devices(self) -> list[Device]:
        try:
            await self._refresh()
        except (RainbirdApiException, OSError, asyncio.TimeoutError) as exc:
            log.warning("Rain Bird: %s", exc)
            await self.close()
            return [Device(id=DEVICE_ID, brand=self.brand, kind="sprinkler", name="Sprinklers",
                           model=self._model, online=False, media="Not responding")]
        st = self._state
        running = st.active_station if st.irrigation_state else 0
        return [Device(
            id=DEVICE_ID, brand=self.brand, kind="sprinkler", name="Sprinklers", model=self._model,
            on=bool(running), media=status_text(st, self.zone_label),
            zones=[{"id": str(z), "label": self.zone_label(z), "running": z == running,
                    "name": (self.config.get("zone_names") or {}).get(str(z), "")} for z in self._zones],
            rain_delay=st.delay_setting, details=self._programs, capabilities=["zones"],
        )]

    def names(self) -> dict[str, str]:
        return {DEVICE_ID: "Sprinklers"} if self.active else {}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        ctl = await self._controller()
        try:
            if state.zone is not None:
                if state.zone not in self._zones:
                    raise HubError(400, f"Zone {state.zone} isn't set up on this controller.")
                await ctl.irrigate_zone(state.zone, state.minutes or 10)
            elif state.action == "stop" or state.on is False:
                await ctl.stop_irrigation()
            elif state.rain_delay_days is not None:
                await ctl.set_rain_delay(state.rain_delay_days)
            else:
                raise HubError(400, "Pick a zone to run, Stop, or a rain delay.")
        except RainbirdApiException as exc:
            raise HubError(502, f"The controller didn't accept that: {exc}") from exc
        self._state_at = float("-inf")  # never

    async def status(self) -> dict:
        return {"connected": self.active, "available": AVAILABLE, "devices": 1 if self.active else 0,
                "zones": len(self._zones), "host": self.config.get("host")}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = self._ctl = None
