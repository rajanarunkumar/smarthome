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
from datetime import datetime, timedelta
from datetime import time as dtime
from types import SimpleNamespace
from typing import Any, Callable

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

WATCH_SECONDS = 30
IDLE_WATCH_SECONDS = 60     # while nobody uses the app: runs last minutes, so a minute's resolution is plenty
MIN_LIBRARY = (6, 1)   # older pyrainbird speaks only HTTP; newer LNK2 firmware answers only HTTPS


def library_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("pyrainbird")
    except Exception:  # noqa: BLE001
        return None


def library_too_old(ver: str | None) -> bool:
    try:
        return tuple(int(x) for x in (ver or "").split(".")[:2]) < MIN_LIBRARY
    except ValueError:
        return False
HUB_RUN_WINDOW = 4 * 3600  # a run counts as hub-started if the hub asked for that zone this recently
DAY_NAMES = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
HUB_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LOCKOUT_MINUTES = 16      # the controller says "wait 15 minutes"; one more so we don't knock on the minute
BACKOFF_MAX_SECONDS = 300


def error_kind(exc: Exception) -> str:
    """lockout | auth | busy | certificate | nack | connection | other, from the library's exception types."""
    names = {c.__name__ for c in type(exc).__mro__}
    for kind, name in (("lockout", "RainbirdAuthLockoutException"), ("auth", "RainbirdAuthException"),
                       ("busy", "RainbirdDeviceBusyException"), ("certificate", "RainbirdCertificateError"),
                       ("nack", "RainbirdDeviceNackError"), ("connection", "RainbirdConnectionError")):
        if name in names:
            return kind
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return "connection"
    return "other"


def explain(exc: Exception, host: str) -> str:
    """What went wrong with the controller, and what to do, in plain words."""
    kind = error_kind(exc)
    if kind == "lockout":
        return ("The controller has locked itself after repeated wrong passwords. Wait 15 minutes, or unplug the controller for "
                "a few seconds to reboot it. The hub has stopped trying so it doesn't extend the lockout.")
    if kind == "auth":
        return ("The controller rejected the password. It's the local password set for the Wi-Fi module in the Rain Bird app, "
                "not your Rain Bird account password. The hub has stopped trying; connect again with the right one.")
    if kind == "busy":
        return ("The controller is busy with another request. It handles one at a time, and the Rain Bird app counts. "
                "Close the Rain Bird app and try again.")
    if kind == "certificate":
        return "The controller's secure connection failed its certificate check."
    if kind == "nack":
        return "The controller refused that command (NACK). This model may not support it."
    if kind == "connection":
        return (f"Can't reach {host or 'the controller'}. Check the IP address (your router's device list shows the Rain Bird "
                "module), that this Mac is on the same Wi-Fi as the controller, and that the module's light shows it's online.")
    return f"{type(exc).__name__}: {exc}"


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


def zone_schedule_dict(zs: Any) -> dict:
    """A controller zone schedule (ESP-RZXe / ST8) as plain data for the app."""
    freq = getattr(zs.frequency, "name", str(zs.frequency))
    return {
        "minutes": int(zs.duration.total_seconds() // 60),
        "starts": [s.strftime("%H:%M") for s in (zs.starts or []) if s is not None],
        "days": [HUB_DAYS[DAY_NAMES.index(d.name)] for d in (zs.days_of_week or []) if d.name in DAY_NAMES],
        "frequency": freq,
    }


def _runs_on(freq: str, days_of_week, day) -> bool:
    if freq == "CUSTOM":
        return DAY_NAMES[day.weekday()] in {d.name for d in (days_of_week or [])}
    if freq == "ODD":
        return day.day % 2 == 1 and day.day != 31
    if freq == "EVEN":
        return day.day % 2 == 0
    return False  # cyclic and off programs can't be placed on past dates reliably


def scheduled_runs(schedule: Any, first_day, last_day, adjust_pct: int = 100) -> list[dict]:
    """Runs the controller's programs would have made between two dates (inclusive).

    Program-based controllers run a program's zones back to back from each start
    time; zone-scheduled ones (ESP-RZXe) run each zone at its own starts.
    Seasonal adjust scales the minutes, as on the controller.
    """
    runs = []
    day = first_day
    while day <= last_day:
        for program in getattr(schedule, "programs", None) or []:
            freq = getattr(program.frequency, "name", str(program.frequency))
            if not _runs_on(freq, program.days_of_week, day):
                continue
            for start in program.starts or []:
                if start is None:
                    continue
                t = datetime.combine(day, start)
                for zd in program.durations or []:
                    minutes = zd.duration.total_seconds() / 60 * adjust_pct / 100
                    if minutes >= 1:
                        runs.append({"started": t.isoformat(timespec="minutes"), "zone": zd.zone, "minutes": round(minutes, 1)})
                        t += timedelta(minutes=minutes)
        for zone, zs in (getattr(schedule, "zone_schedules", None) or {}).items():
            freq = getattr(zs.frequency, "name", str(zs.frequency))
            minutes = zs.duration.total_seconds() / 60
            if minutes < 1 or not _runs_on(freq, zs.days_of_week, day):
                continue
            for start in zs.starts or []:
                if start is not None:
                    runs.append({"started": datetime.combine(day, start).isoformat(timespec="minutes"),
                                 "zone": zone, "minutes": round(minutes, 1)})
        day += timedelta(days=1)
    return runs


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
        self._program_ids: list[int] = []
        self._zone_scheds: dict[int, dict] = {}
        self._schedule_raw: Any = None
        self.watch_error: str | None = None   # why the poller can't read the controller, shown in Insights
        self._halt_until = float("-inf")      # after a rejected password or a lockout the hub stops knocking until then
        self._state_mode: str | None = None   # combined, or legacy for controllers that don't answer it
        self._lock = asyncio.Lock()
        # Watering log: called with (started, zone, minutes, source) when a run ends.
        self.on_run: Callable[[datetime, int, float, str], None] | None = None
        self._running: tuple[int, datetime] | None = None
        self._hub_started: dict[int, float] = {}
        self._sequence: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None

    @property
    def config(self) -> dict:
        return self.secrets.get("rainbird") or {}

    @property
    def active(self) -> bool:
        return AVAILABLE and bool(self.config.get("host"))

    async def start(self) -> None:
        self._watcher = asyncio.create_task(self._watch())

    async def _read_state(self, ctl: Any) -> Any:
        """The controller's current state. Newer models answer the combined request; for the others, the same
        fields come from the zone and rain-delay requests (without time remaining, which only the combined one has)."""
        if self._state_mode != "legacy":
            try:
                state = await ctl.get_combined_controller_state()
                self._state_mode = "combined"
                return state
            except Exception:
                if self._state_mode == "combined":
                    raise  # it worked before, so this is a real failure and not an unsupported request
        zones = await ctl.get_zone_states()
        active = sorted(zones.active_set)
        try:
            delay = await ctl.get_rain_delay()
        except Exception:  # noqa: BLE001 - optional
            delay = 0
        try:
            sensor = int(bool(await ctl.get_rain_sensor_state()))
        except Exception:  # noqa: BLE001 - optional
            sensor = 0
        self._state_mode = "legacy"
        return SimpleNamespace(irrigation_state=1 if active else 0, active_station=active[0] if active else 0,
                               remaining_runtime=0, delay_setting=delay or 0, sensor_state=sensor, seasonal_adjust=100)

    async def _watch_once(self, delay: float) -> float:
        """One poll. Returns how long to wait before the next one: a rejected password or a lockout stops
        the polling (it would only extend a lockout), and an unreachable controller is retried with a growing delay."""
        if not self.active or self._halted():
            return self._watch_seconds() if not self._halted() else min(delay, 60)
        try:
            async with self._lock:
                ctl = await self._controller()
                self._state = await self._read_state(ctl)
                self._state_at = time.monotonic()
            self.observe(self._state, datetime.now())
            self.watch_error = None
            return self._watch_seconds()
        except Exception as exc:  # noqa: BLE001 - keep watching
            message = explain(exc, self.config.get("host", ""))
            if message != self.watch_error:  # say it once, at warning level, so it isn't silent
                log.warning("Rain Bird watch: %s", message)
            self.watch_error = message
            await self.close()
            kind = error_kind(exc)
            if kind == "lockout":
                self._halt_until = time.monotonic() + LOCKOUT_MINUTES * 60
                return LOCKOUT_MINUTES * 60
            if kind == "auth":
                self._halt_until = float("inf")  # until connect() is given a password
                return 15 * 60
            return min(max(delay, WATCH_SECONDS) * 2, BACKOFF_MAX_SECONDS)

    idle = staticmethod(lambda: False)   # set by the hub: True while nobody uses the app

    def _watch_seconds(self) -> int:
        return IDLE_WATCH_SECONDS if self.idle() else WATCH_SECONDS

    async def _watch(self) -> None:
        """Poll the controller so every run is logged, even ones started by its own programs."""
        delay = WATCH_SECONDS
        while True:
            delay = await self._watch_once(delay)
            await asyncio.sleep(delay)

    def observe(self, state: Any, now: datetime) -> None:
        """Turn controller states into runs: when the active zone changes, the previous run ended."""
        zone = state.active_station if state.irrigation_state else 0
        current = self._running
        if current and current[0] == zone:
            return
        if current and self.on_run:
            started_zone, started = current
            hub = time.monotonic() - self._hub_started.pop(started_zone, float("-inf")) < HUB_RUN_WINDOW
            self.on_run(started, started_zone, (now - started).total_seconds() / 60, "hub" if hub else "controller")
        self._running = (zone, now) if zone else None

    def zone_label(self, zone: int) -> str:
        name = (self.config.get("zone_names") or {}).get(str(zone))
        return f"Zone {zone}" + (f" ({name})" if name else "")

    def _halted(self) -> bool:
        return time.monotonic() < self._halt_until

    async def _controller(self, cfg: dict | None = None) -> Any:
        if cfg is None and self._halted():  # never keep sending a password the controller has refused
            raise HubError(409, self.watch_error or "The hub paused its connection to the controller. Connect again in Settings.")
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
        cfg = {"host": host.strip(), "password": password, "zone_names": self.config.get("zone_names") or {},
               "zone_areas": self.config.get("zone_areas") or {}}
        await self.close()
        try:
            await self._controller(cfg)
        except Exception as exc:  # noqa: BLE001 - every library failure gets a plain explanation
            await self.close()
            kind = error_kind(exc)
            raise HubError({"lockout": 423, "auth": 401}.get(kind, 504), explain(exc, host)) from exc
        self.secrets.set("rainbird", cfg)
        self._halt_until, self.watch_error = float("-inf"), None  # a good password: resume polling
        return {"connected": True, "devices": 1, "zones": len(self._zones), "model": self._model}

    async def diagnose(self, host: str, password: str) -> list[dict]:
        """Step-by-step connection test: is the address reachable, then does the controller accept the password.
        It makes at most one controller request, so testing can't trigger a lockout by itself."""
        steps: list[dict] = []
        if host == self.config.get("host") and self._halted():
            return [{"ok": False, "title": "The hub has paused its own connection", "detail": self.watch_error or "Connect again in Settings."}]
        ports = {}
        for port in (443, 80):
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
                writer.close()
                ports[port] = True
            except (OSError, asyncio.TimeoutError):
                ports[port] = False
        if not any(ports.values()):
            return [{"ok": False, "title": f"Nothing answers at {host}",
                     "detail": "Ports 443 and 80 are both closed or silent. Check the IP address (your router's device list shows the Rain Bird "
                               "Wi-Fi module, often named RainBird or LNK; it may have changed), that the module's light shows it's online, and "
                               "that this Mac is on the same network as the controller. If your router puts smart devices on a separate IoT or "
                               "guest network, the Rain Bird app still works (it goes through Rain Bird's cloud) but the hub can't reach it. "
                               "On the Mac, python -m hub.adapters.rainbird_adapter --find searches the network for it."}]
        steps.append({"ok": True, "title": f"{host} is reachable", "detail": f"Port {443 if ports[443] else 80} is open."})
        if not AVAILABLE:
            steps.append({"ok": False, "title": "Rain Bird support isn't installed", "detail": "It needs Python 3.13 or newer on the hub Mac."})
            return steps
        ver = library_version()
        if library_too_old(ver):
            steps.append({"ok": False, "title": f"Rain Bird library {ver} is too old",
                          "detail": "It can't use the secure (HTTPS) connection newer Rain Bird Wi-Fi modules require, so the right password "
                                    "still fails. On the Mac: pip install -U -r requirements.txt, then restart the hub."})
        session = aiohttp.ClientSession()
        try:
            ctl = await create_controller(session, host, password or "")
            model = await ctl.get_model_and_version()
            zones = await ctl.get_available_stations()
            steps.append({"ok": True, "title": "The controller accepted the connection",
                          "detail": f"{model.model_name}, {len(zones.active_set)} zone(s)."})
        except Exception as exc:  # noqa: BLE001
            steps.append({"ok": False, "title": "The controller didn't accept the connection", "detail": explain(exc, host)})
        finally:
            await session.close()
        return steps

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
                self._state = await self._read_state(ctl)
                self._state_at = now
            if now - self._programs_at > SCHEDULE_SECONDS:
                try:
                    schedule = await ctl.get_schedule()
                    self._schedule_raw = schedule
                    self._programs = [p for p in (describe_program(x) for x in schedule.programs) if p]
                    self._program_ids = [x.program for x in schedule.programs if describe_program(x)]
                    self._zone_scheds = {z: zone_schedule_dict(zs) for z, zs in (schedule.zone_schedules or {}).items()}
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
                    "name": (self.config.get("zone_names") or {}).get(str(z), ""),
                    "area_sqft": (self.config.get("zone_areas") or {}).get(str(z)),
                    "schedule": self._zone_scheds.get(z)} for z in self._zones],
            actions=[{"id": f"program_{pid}", "label": f"Run {p['label']} now"}
                     for pid, p in zip(self._program_ids, self._programs)],
            rain_delay=st.delay_setting, details=self._programs,
            capabilities=["zones"] + (["actions"] if self._program_ids else []),
        )]

    def names(self) -> dict[str, str]:
        return {DEVICE_ID: "Sprinklers"} if self.active else {}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        try:
            async with self._lock:  # the controller handles one request at a time; more than one gets a 503
                await self._set_state_locked(await self._controller(), state)
        except (RainbirdApiException, OSError, asyncio.TimeoutError) as exc:
            raise HubError(502, explain(exc, self.config.get("host", ""))) from exc
        self._state_at = float("-inf")  # never

    async def _set_state_locked(self, ctl: Any, state: StateIn) -> None:
        if state.zone is not None:
            if state.zone not in self._zones:
                raise HubError(400, f"Zone {state.zone} isn't set up on this controller.")
            self._hub_started[state.zone] = time.monotonic()
            await ctl.irrigate_zone(state.zone, state.minutes or 10)
        elif state.action == "stop" or state.on is False:
            if self._sequence and not self._sequence.done():
                self._sequence.cancel()
            await ctl.stop_irrigation()
        elif state.action and state.action.startswith("program_"):
            program = int(state.action.split("_", 1)[1])
            if program not in self._program_ids:
                raise HubError(400, "That program isn't set up on the controller.")
            await ctl.set_program(program)
        elif state.rain_delay_days is not None:
            await ctl.set_rain_delay(state.rain_delay_days)
        else:
            raise HubError(400, "Pick a zone to run, Stop, or a rain delay.")

    async def past_runs(self, first_day, last_day) -> list[dict]:
        """What the controller's programs would have run on past days (for the 4-week backfill)."""
        await self._refresh()
        if self._schedule_raw is None:
            return []
        adjust = getattr(self._state, "seasonal_adjust", 100) or 100
        return scheduled_runs(self._schedule_raw, first_day, last_day, adjust)

    def set_zone_area(self, zone: int, area_sqft: float | None) -> None:
        cfg = dict(self.config)
        areas = dict(cfg.get("zone_areas") or {})
        if area_sqft:
            areas[str(zone)] = float(area_sqft)
        else:
            areas.pop(str(zone), None)
        self.secrets.set("rainbird", {**cfg, "zone_areas": areas})

    def zone_areas(self) -> dict[int, float]:
        return {int(z): a for z, a in (self.config.get("zone_areas") or {}).items()}

    @property
    def zone_scheduled(self) -> bool:
        """ESP-RZXe / ST8 style: each zone has its own schedule the hub can write."""
        return bool(self._zone_scheds)

    async def set_zone_schedule(self, zone: int, minutes: int, starts: list[str], days: list[str]) -> dict:
        if zone not in self._zones:
            raise HubError(404, f"Zone {zone} isn't set up on this controller.")
        if not self.zone_scheduled:
            raise HubError(400, "This controller uses programs (A, B, C…) that the Rain Bird library can't write. "
                                "Use a smart program in the hub instead.")
        if len(starts) > 6:
            raise HubError(400, "A zone can have at most 6 start times.")
        from pyrainbird.const import DayOfWeek, ProgramFrequency

        start_times = sorted(dtime.fromisoformat(s) for s in starts)
        week = {getattr(DayOfWeek, DAY_NAMES[HUB_DAYS.index(d)]) for d in days}
        try:
            async with self._lock:
                ctl = await self._controller()
                await ctl.set_zone_schedule(zone, timedelta(minutes=minutes), start_times, ProgramFrequency.CUSTOM, week)
                saved = (await ctl.get_schedule()).zone_schedules.get(zone)
        except (RainbirdApiException, OSError, asyncio.TimeoutError) as exc:
            raise HubError(502, explain(exc, self.config.get("host", ""))) from exc
        self._programs_at = float("-inf")
        result = zone_schedule_dict(saved) if saved else None
        # Start times are stored in 10-minute steps, so compare what was asked to what came back.
        wanted = {"minutes": minutes, "days": sorted(days, key=HUB_DAYS.index)}
        if result is None or result["minutes"] != wanted["minutes"] or \
                sorted(result["days"], key=HUB_DAYS.index) != wanted["days"]:
            raise HubError(502, "The controller saved something different from what was sent. "
                                "Check the schedule in the Rain Bird app.")
        return result

    def run_sequence(self, zones: list[tuple[int, int]], source: str = "hub") -> None:
        """Run zones one after another (a smart program). Stop cancels it."""
        if self._sequence and not self._sequence.done():
            self._sequence.cancel()

        async def go() -> None:
            for zone, minutes in zones:
                if minutes <= 0:
                    continue
                self._hub_started[zone] = time.monotonic()
                async with self._lock:
                    ctl = await self._controller()
                    await ctl.irrigate_zone(zone, minutes)
                await asyncio.sleep(minutes * 60 + 20)

        self._sequence = asyncio.create_task(go())

    async def status(self) -> dict:
        return {"connected": self.active, "available": AVAILABLE, "zone_scheduled": self.zone_scheduled, "devices": 1 if self.active else 0,
                "zones": len(self._zones), "host": self.config.get("host"),
                "watch_error": self.watch_error, "paused": self._halted()}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = self._ctl = None


# ---- command line: python -m hub.adapters.rainbird_adapter [IP] [--find] ----
def _local_address(host: str) -> str | None:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 80))      # no packet is sent; this only picks the interface that would be used
            return s.getsockname()[0]
    except OSError:
        return None


async def _open_ports(host: str, ports=(443, 80), timeout: float = 0.8) -> list[int]:
    found = []
    for port in ports:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
            writer.close()
            found.append(port)
        except (OSError, asyncio.TimeoutError):
            pass
    return found


async def find_controllers(password: str, subnet: str) -> list[tuple[str, str]]:
    """Every address on the subnet with port 443 or 80 open gets one connection attempt. Only a Rain Bird
    answers with a model name; other devices just fail. Returns [(ip, model)]."""
    import ipaddress
    hosts = [str(h) for h in ipaddress.ip_network(subnet, strict=False).hosts()]
    sem = asyncio.Semaphore(64)

    async def probe(ip: str) -> str | None:
        async with sem:
            return ip if await _open_ports(ip) else None
    candidates = [ip for ip in await asyncio.gather(*(probe(h) for h in hosts)) if ip]
    print(f"  {len(candidates)} address(es) with a web port open: {', '.join(candidates) or 'none'}")
    found = []
    async with aiohttp.ClientSession() as session:
        for ip in candidates:
            try:
                ctl = await asyncio.wait_for(create_controller(session, ip, password), timeout=8)
                model = await asyncio.wait_for(ctl.get_model_and_version(), timeout=8)
                found.append((ip, model.model_name))
                print(f"  {ip}: Rain Bird {model.model_name}")
            except Exception as exc:  # noqa: BLE001
                if error_kind(exc) in ("auth", "lockout", "busy"):
                    found.append((ip, f"Rain Bird (answered: {error_kind(exc)})"))
                    print(f"  {ip}: looks like a Rain Bird, but: {explain(exc, ip)}")
    return found


def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass
    import platform
    parser = argparse.ArgumentParser(description="Test the hub's connection to a Rain Bird controller. Sends at most one "
                                                 "password attempt to the controller (or one per candidate with --find).")
    parser.add_argument("host", nargs="?", help="controller IP address (default: the one saved in the hub)")
    parser.add_argument("--find", action="store_true", help="search this Mac's network (/24) for the controller")
    args = parser.parse_args(argv)
    saved = Secrets().get("rainbird") or {}
    print(f"Python {platform.python_version()} · pyrainbird {library_version() or 'not installed'}")
    if not AVAILABLE:
        print("Rain Bird support isn't installed: it needs Python 3.13+. Recreate the hub's venv with python3.13 "
              "and pip install -r requirements.txt.")
        return 2
    if library_too_old(library_version()):
        print("pyrainbird is too old for controllers that only speak HTTPS. Run: pip install -U -r requirements.txt")
    host = args.host or saved.get("host")
    password = getpass.getpass("Controller password (Enter = the one saved in the hub): ") or saved.get("password", "")
    if args.find or not host:
        import ipaddress
        local = _local_address(host or "1.1.1.1")
        if not local:
            print("This Mac has no network address.")
            return 1
        subnet = str(ipaddress.ip_network(local + "/24", strict=False))
        print(f"Searching {subnet} (this Mac is {local})…")
        found = asyncio.run(find_controllers(password, subnet))
        if not found:
            print("No Rain Bird found. If the Rain Bird app works, the controller is probably on another network "
                  "(IoT or guest Wi-Fi) or a different subnet. Look for it in your router's device list.")
            return 1
        print("Use this address in Settings → Rain Bird:", ", ".join(ip for ip, _ in found))
        return 0
    local = _local_address(host)
    print(f"Controller {host} · this Mac {local or 'unknown'}")
    if local and local.rsplit(".", 1)[0] != host.rsplit(".", 1)[0]:
        print("Note: the controller is on a different subnet from this Mac. That only works if your router routes between them.")
    if host == saved.get("host"):
        print("Tip: stop the hub first (or expect 'busy'), since the controller answers one request at a time.")
    steps = asyncio.run(RainBirdAdapter(Secrets()).diagnose(host, password))
    for step in steps:
        print(("OK   " if step["ok"] else "FAIL ") + step["title"])
        print("     " + step["detail"])
    return 0 if all(s["ok"] for s in steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
