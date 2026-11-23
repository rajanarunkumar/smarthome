"""HTTP API and web app for the hub."""

from __future__ import annotations

import asyncio
import hmac
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .adapters.base import Adapter
from .adapters.dyson_adapter import DysonAdapter
from .adapters.ecovacs_adapter import EcovacsAdapter
from .adapters.govee_adapter import GoveeAdapter
from .adapters.hue_adapter import HueAdapter
from .adapters.kasa_adapter import KasaAdapter
from .adapters.lennox_adapter import LennoxAdapter
from .adapters.lgtv_adapter import LgTvAdapter
from .adapters.nest_adapter import NestAdapter
from .adapters.pentair_adapter import PentairAdapter
from .adapters.rainbird_adapter import RainBirdAdapter
from .adapters.ring_adapter import RingAdapter
from .adapters.schlage_adapter import SchlageAdapter
from .adapters.smt_adapter import SmartMeterTexasAdapter
from .adapters.sonos_adapter import SonosAdapter
from .adapters.thinq_adapter import ThinQAdapter
from .adapters.water_adapter import WaterAdvisorAdapter
from .auth import SESSION_COOKIE, Auth
from .cache import LoadingCache
from .telemetry import Telemetry
from . import backup as backup_mod
from .config import Secrets, data_dir
from . import weather as weather_api
from .energy import EnergyStore, build_energy, device_power
from .hvac import ACTIONS as HVAC_ACTIONS, HvacStore, build_hvac
from .pool import ACTIONS as POOL_ACTIONS, PoolStore, build_pool
from .tou import tou_report
from .bills import BillStore, bills_report, import_folder, tariff_from
from .history import HistoryStore, align_hourly, build_insights, confirm_with_meter, missing_runs
from .irrigation import SmartPrograms
from .flowtest import FlowTests
from .history import minutes_by_hour
from .photos import PhotoStore, assess_with_claude, zone_advice
from .scheduler import HubScheduler
from .models import DAYS, Device, DevicePatch, HubError, Schedule, ScheduleIn, StateIn

log = logging.getLogger("hub")
WEB_DIR = Path(__file__).parent / "web"
DEVICE_TIMEOUT = 4.0       # a request waits this long for one brand, then shows its last known state
LOAD_CAP = 60.0            # a brand's load is abandoned after this long
RAW_TIMEOUT = 15.0         # manual "refresh raw data" waits longer
RAW_MIN_GAP = 10.0         # per-brand rate limit for manual raw refreshes
LOCKOUT_PAUSE = 60.0
DISCOVERY_BRANDS = ("kasa", "sonos", "govee")
# Settings routes under /api/<segment>/… that change a brand's account or pairing.
PATH_BRAND = {"lgtv": "lg"}


class Hub:
    def __init__(self) -> None:
        from .store import Store

        self.secrets = Secrets()
        self.store = Store(data_dir() / "hub.db")
        self.auth = Auth(self.secrets, self.store.db)
        self.kasa = KasaAdapter()
        self.hue = HueAdapter(self.secrets)
        self.sonos = SonosAdapter()
        self.govee = GoveeAdapter(self.secrets)
        self.lgtv = LgTvAdapter(self.secrets)
        self.thinq = ThinQAdapter(self.secrets)
        self.dyson = DysonAdapter(self.secrets)
        self.ecovacs = EcovacsAdapter(self.secrets)
        self.pentair = PentairAdapter(self.secrets)
        self.schlage = SchlageAdapter(self.secrets)
        self.smt = SmartMeterTexasAdapter(self.secrets)
        self.water = WaterAdvisorAdapter(self.secrets)
        self.rainbird = RainBirdAdapter(self.secrets)
        self.ring = RingAdapter(self.secrets)
        self.nest = NestAdapter(self.secrets)
        self.lennox = LennoxAdapter(self.secrets)
        self.adapters: list[Adapter] = [self.kasa, self.hue, self.sonos, self.govee, self.lgtv, self.thinq,
                                        self.dyson, self.ecovacs, self.pentair, self.schlage, self.smt, self.water,
                                        self.rainbird, self.ring, self.nest, self.lennox]
        self.scheduler = HubScheduler(self.store.db, self._apply)
        self.adapters.append(self.scheduler)
        self.last_devices: dict[str, Device] = {}
        self.history = HistoryStore(self.store.db)
        self.rainbird.on_run = self.history.log_run
        self.irrigation = SmartPrograms(self.store.db, self.history, self.rainbird)
        self.energy = EnergyStore(self.store.db)
        self.pool = PoolStore(self.store.db)
        self.bills = BillStore(self.store.db)
        self.hvac = HvacStore(self.store.db)
        # Nest events land in the HVAC samples the moment heating or cooling starts or stops.
        from datetime import datetime
        self.nest.on_update = lambda device_id, name, th: self.hvac.record(device_id, name, th, datetime.now())
        self.photos = PhotoStore(self.store.db, data_dir() / "photos")
        self.flowtests = FlowTests(self.store.db, self.rainbird)
        self.backups = backup_mod.Backups(self.secrets, self.store.db, data_dir(),
                                          client_factory=lambda cfg: backup_mod.make_client(cfg))
        self._collector: asyncio.Task | None = None
        self._recorder: asyncio.Task | None = None
        self._warmer: asyncio.Task | None = None
        self._by_brand = {a.brand: a for a in self.adapters}
        self.telemetry = Telemetry(data_dir())
        # L1 caches (see hub/cache.py): a slow brand never holds up the others.
        self.device_cache = LoadingCache("devices", self._load_brand_devices, max_size=64,
                                         refresh_after=10, expire_after=120, timeout=DEVICE_TIMEOUT)
        self.scene_cache = LoadingCache("scenes", self._load_scenes, max_size=4,
                                        refresh_after=30, expire_after=600, timeout=8)
        self.schedule_cache = LoadingCache("schedules", self._load_schedules, max_size=4,
                                           refresh_after=30, expire_after=600, timeout=8)
        self.caches = {c.name: c for c in (self.device_cache, self.scene_cache, self.schedule_cache)}
        self._devices_wanted = float("-inf")
        self._raw_last: dict[str, float] = {}
        self._raw_task: asyncio.Task | None = None
        self._refresh_soon: dict[str, asyncio.Task] = {}

    async def collect_once(self, days: int = 3) -> dict:
        """Pull weather, water, electricity and past sprinkler runs into the database.

        `days` is how far back to fetch water and electricity (28 for the 4-week
        backfill). Returns counts of what was stored and anything that failed.
        """
        from datetime import date, timedelta

        report: dict = {"problems": []}
        problems = report["problems"]
        location = self.secrets.get("location")
        tz_name = (location or {}).get("timezone", "America/Chicago")
        if location:
            try:
                rows = await weather_api.daily(location["latitude"], location["longitude"])
                self.history.put_weather(rows)
                report["weather_days"] = len(rows)
            except HubError as exc:
                problems.append(f"Weather: {exc.message}")
        if self.water.active:
            try:
                daily, hourly = await self.water.history(days=days, tz_name=tz_name)
                self.history.put_water_daily(daily)
                self.history.put_water_hourly(hourly)
                report["water_days"], report["water_hours"] = len(daily), len(hourly)
                report["hourly_days_with_data"] = self.water.diagnostics.get("hourly_days_with_data")
                from datetime import datetime
                since = date.today() - timedelta(days=30)
                busy = set(minutes_by_hour(self.history.runs(since)))
                report["flow_tests_measured"] = self.flowtests.update_measurements(
                    self.history.aligned_hourly(since, None, tz_name)[0], busy, datetime.now())
            except HubError as exc:
                problems.append(f"Water: {exc.message}")
        if self.smt.active:
            try:
                have = self.energy.interval_days(date.today() - timedelta(days=days + 1))
                rows = await self.smt.intervals(days=days, have=have)
                self.energy.put_intervals(rows)
                report["energy_slots"] = len(rows)
            except HubError as exc:
                problems.append(f"Electricity: {exc.message}")
            except Exception as exc:  # noqa: BLE001 - the library raises its own errors
                problems.append(f"Electricity: {exc}")
        if self.rainbird.active:
            try:
                first, last = date.today() - timedelta(days=28), date.today() - timedelta(days=1)
                recorded = [r for r in self.history.runs(first) if r["source"] != "inferred"]
                # Replay the controller's programs for every day the hub saw nothing: before it started watching,
                # and any day the Mac was asleep or the hub restarting at watering time.
                replayed = missing_runs(await self.rainbird.past_runs(first, last), recorded)
                # Work out the meter's clock from the unfiltered replay first; a meter that is off would otherwise
                # reject every replayed run and leave nothing to detect the offset from.
                hourly, _ = align_hourly(self.history.water_hourly(first), recorded + replayed, tz_name)
                runs = confirm_with_meter(replayed, hourly) if hourly else []
                self.history.replace_inferred(runs, first, last)
                report["sprinkler_runs_reconstructed"] = len(runs)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"Sprinkler history: {exc}")
        return report

    async def _collect_loop(self) -> None:
        days = 28  # first pass after start: the 4-week backfill
        while True:
            started, t0 = time.time(), time.monotonic()
            try:
                report = await self.collect_once(days=days)
                for p in report["problems"]:
                    log.warning("History: %s", p)
                self.telemetry.job("history collector", started, (time.monotonic() - t0) * 1000,
                                   not report["problems"], "; ".join(report["problems"]) or f"{days} days", 3 * 3600)
                days = 3
            except Exception as exc:  # noqa: BLE001
                log.warning("History collector: %s", exc)
                self.telemetry.job("history collector", started, (time.monotonic() - t0) * 1000, False, str(exc), 3 * 3600)
            await asyncio.sleep(3 * 3600)

    async def _record_loop(self) -> None:
        """Every 2 minutes, note which devices are running, for the energy analytics."""
        from datetime import datetime
        while True:
            await asyncio.sleep(120)
            started, t0 = time.time(), time.monotonic()
            try:
                devices, _ = await self.devices(interactive=False)
                self.energy.observe(devices, datetime.now())
                self.hvac.record_devices(devices, datetime.now())
                for pump in self.pentair.readings():
                    self.pool.record(pump["reading"], datetime.now())
                self.telemetry.job("activity recorder", started, (time.monotonic() - t0) * 1000, True,
                                   f"{len(devices)} devices", 120)
            except Exception as exc:  # noqa: BLE001
                log.debug("Activity recorder: %s", exc)
                self.telemetry.job("activity recorder", started, (time.monotonic() - t0) * 1000, False, str(exc), 120)

    async def _warm_loop(self) -> None:
        """Keep the device cache warm: every 15 s while someone uses the app, every 5 min otherwise."""
        while True:
            started, t0 = time.time(), time.monotonic()
            try:
                idle = time.monotonic() - self._devices_wanted > 600
                keys = [a.brand for a in self.active_adapters()
                        if not idle or (self.device_cache.age(a.brand) or float("inf")) > 300]
                n = self.device_cache.warm(keys)
                self.telemetry.job("cache warmer", started, (time.monotonic() - t0) * 1000, True,
                                   f"{n} refresh(es) started" + (" (idle)" if idle else ""), 15)
            except Exception as exc:  # noqa: BLE001
                self.telemetry.job("cache warmer", started, (time.monotonic() - t0) * 1000, False, str(exc), 15)
            await asyncio.sleep(15)

    def photo_advice(self, zone: int, week_in, need_in, tmean7) -> dict | None:
        photos = self.photos.for_zone(zone, limit=2)
        if not photos:
            return {**zone_advice(None, None, None, week_in, need_in, tmean7), "photo": None}
        latest = photos[0]
        prev = photos[1]["green_share"] if len(photos) > 1 else None
        advice = zone_advice(latest["green_share"], prev, latest["ai"], week_in, need_in, tmean7)
        return {**advice, "photo": {"id": latest["id"], "taken": latest["taken"], "green_share": latest["green_share"],
                                    "ai": latest["ai"]}}

    def claude_context(self, zone: int) -> str:
        """What Claude is told alongside a zone photo: zone, size and the last week's water and weather."""
        from datetime import date, timedelta
        data = build_insights(self.history, self.rainbird.zone_label, self.rainbird.zone_areas(), 7,
                              measured=self.flowtests.measured_gpm())
        z = next((x for x in data["zones"] if x["zone"] == zone), {})
        lines = [f"Zone: {self.rainbird.zone_label(zone)}", f"Date: {date.today().isoformat()}"]
        if z.get("area_sqft"):
            lines.append(f"Area: {z['area_sqft']:.0f} sq ft")
        s = data["summary"]
        lines.append(f"Last 7 days: rain {s['rain_in']} in, reference evapotranspiration {s['et0_in']} in"
                     + (f", mean temperature {data['tmean7_f']} °F" if data.get("tmean7_f") else ""))
        if z.get("week_in") is not None and data.get("need_week_in") is not None:
            lines.append(f"Irrigation applied to this zone in the last 7 days: {z['week_in']} in; estimated need {data['need_week_in']} in")
        lines.append("Assess this zone's watering from the photo.")
        return "\n".join(lines)

    def start_background(self) -> None:
        self.flowtests.start()
        self.backups.start()
        self.irrigation.start()
        self._collector = asyncio.create_task(self._collect_loop())
        self._recorder = asyncio.create_task(self._record_loop())
        self._warmer = asyncio.create_task(self._warm_loop())
        self.telemetry.start()
        logging.getLogger().addHandler(self.telemetry.logs)
        self._by_prefix = {p: a for a in self.adapters for p in a.prefixes}

    async def _apply(self, device_id: str, state: StateIn) -> None:
        await self.set_state(device_id, state)

    async def set_state(self, device_id: str, state: StateIn) -> None:
        """Change a device, then update the cached copy so the app shows it straight away."""
        adapter = self.adapter_for(device_id)
        op = self.telemetry.op(adapter.brand, "set_state")
        t0 = time.monotonic()
        try:
            await adapter.set_state(device_id, state)
        except Exception as exc:
            op.record((time.monotonic() - t0) * 1000, str(getattr(exc, "message", None) or exc))
            raise
        op.record((time.monotonic() - t0) * 1000)
        self._patch_cached(adapter.brand, device_id, state)
        self.refresh_brand_soon(adapter.brand)

    def _patch_cached(self, brand: str, device_id: str, state: StateIn) -> None:
        for d in self.device_cache.get_if_present(brand) or []:
            if d.id != device_id:
                continue
            if state.on is not None:
                d.on = state.on
                if d.kind == "speaker":
                    d.playing = state.on
            for f in ("brightness", "volume", "muted", "locked"):
                v = getattr(state, f)
                if v is not None and hasattr(d, f):
                    setattr(d, f, v)

    def refresh_brand_soon(self, brand: str, delay: float = 1.5) -> None:
        """Re-read one brand shortly after a change, so the cache settles on the device's real state."""
        if brand in self._refresh_soon and not self._refresh_soon[brand].done():
            return

        async def later():
            await asyncio.sleep(delay)
            try:
                await self.device_cache.refresh(brand)
            except Exception:  # noqa: BLE001 - shown via cache stats and the next read
                pass

        self._refresh_soon[brand] = asyncio.create_task(later())

    async def _load_scenes(self, _key=None) -> list:
        results = await asyncio.gather(*(a.scenes() for a in self.active_adapters()))
        return [scene for scenes in results for scene in scenes]

    async def _load_schedules(self, _key=None) -> tuple[list[Schedule], list[str]]:
        adapters = self.active_adapters()
        results = await asyncio.gather(*(a.schedules() for a in adapters), return_exceptions=True)
        schedules: list[Schedule] = []
        errors: list[str] = []
        for adapter, result in zip(adapters, results):
            if isinstance(result, BaseException):
                errors.append(f"{adapter.brand}: {getattr(result, 'message', result)}")
            else:
                schedules.extend(result)
        return schedules, errors

    def invalidate_brand(self, brand: str) -> None:
        self.device_cache.invalidate(brand)
        self.scene_cache.invalidate_all()
        self.schedule_cache.invalidate_all()

    async def raw_refresh(self, brands: list[str] | None = None, collect: bool = False,
                          backfill_days: int | None = None) -> dict:
        """Go back to the devices and clouds, bypassing the caches (Admin → Refresh raw data).
        Single flight: a second call while one runs joins it."""
        if self._raw_task and not self._raw_task.done():
            return await asyncio.shield(self._raw_task)
        self._raw_task = asyncio.create_task(self._raw_refresh(brands, collect, backfill_days))
        return await asyncio.shield(self._raw_task)

    async def _raw_refresh(self, brands, collect, backfill_days) -> dict:
        active = {a.brand: a for a in self.active_adapters() if a.brand != "hub"}
        wanted = [b for b in (brands or list(active)) if b in active]

        async def one(brand: str) -> dict:
            now = time.monotonic()
            wait = self._raw_last.get(brand, float("-inf")) - now
            if wait > 0:
                return {"ok": False, "skipped": True, "error": f"refreshed moments ago; try again in {wait:.0f} s"}
            self._raw_last[brand] = now + RAW_MIN_GAP
            t0 = time.monotonic()
            try:
                adapter = active[brand]
                if brand in DISCOVERY_BRANDS:
                    await asyncio.wait_for(adapter.discover(), RAW_TIMEOUT)
                result = await self.device_cache.refresh(brand, timeout=RAW_TIMEOUT)
                return {"ok": True, "ms": round((time.monotonic() - t0) * 1000), "devices": len(result)}
            except Exception as exc:  # noqa: BLE001
                text = "no answer in time" if isinstance(exc, asyncio.TimeoutError) else str(getattr(exc, "message", None) or exc)
                if "lock" in text.lower():
                    self._raw_last[brand] = time.monotonic() + LOCKOUT_PAUSE
                return {"ok": False, "ms": round((time.monotonic() - t0) * 1000), "error": text}

        results = await asyncio.gather(*(one(b) for b in wanted))
        out: dict = {"brands": dict(zip(wanted, results))}
        if brands is None:
            self.scene_cache.invalidate_all()
            self.schedule_cache.invalidate_all()
        if collect or backfill_days:
            t0 = time.monotonic()
            try:
                report = await self.collect_once(days=backfill_days or 3)
                out["history"] = {"ok": not report["problems"], "ms": round((time.monotonic() - t0) * 1000), **report}
            except Exception as exc:  # noqa: BLE001
                out["history"] = {"ok": False, "ms": round((time.monotonic() - t0) * 1000), "error": str(exc)}
        return out

    def adapter_for(self, item_id: str) -> Adapter:
        adapter = self._by_prefix.get(item_id.split(":", 1)[0])
        if adapter is None:
            raise HubError(404, f"Unknown item {item_id}")
        return adapter

    def active_adapters(self) -> list[Adapter]:
        return [a for a in self.adapters if a.active]

    async def _load_brand_devices(self, brand: str) -> list[Device]:
        """Cache loader: ask one brand for its devices, timed for the Admin tab."""
        adapter = self._by_brand[brand]
        op = self.telemetry.op(brand, "devices")
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(adapter.devices(), LOAD_CAP)
        except asyncio.TimeoutError:
            op.record((time.monotonic() - t0) * 1000, f"no answer in {LOAD_CAP:.0f} s", timeout=True)
            raise
        except Exception as exc:
            op.record((time.monotonic() - t0) * 1000, str(getattr(exc, "message", None) or exc))
            raise
        op.record((time.monotonic() - t0) * 1000)
        return result

    async def devices(self, fresh: bool = False, interactive: bool = True) -> tuple[list[Device], list[str]]:
        """All devices. Served from the L1 cache: each brand gets DEVICE_TIMEOUT seconds, after which
        its last known devices are shown (or it's reported as still loading)."""
        if interactive:
            self._devices_wanted = time.monotonic()
        adapters = self.active_adapters()
        cache = self.device_cache

        async def one(a: Adapter):
            if fresh:
                return await cache.refresh(a.brand, timeout=RAW_TIMEOUT)
            return await cache.get(a.brand)

        results = await asyncio.gather(*(one(a) for a in adapters), return_exceptions=True)
        devices: list[Device] = []
        errors: list[str] = []
        for adapter, result in zip(adapters, results):
            entry = cache.entry(adapter.brand)
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.TimeoutError):
                    msg = "slow to answer, still trying"
                else:
                    msg = str(getattr(result, "message", None) or result)
                    log.warning("%s: %s", adapter.brand, msg)
                if entry is not None:  # fresh=True failed: keep showing what we had
                    devices.extend(entry.value)
                    msg += ", showing last known state"
                errors.append(f"{adapter.brand}: {msg}")
            else:
                devices.extend(result)
                if entry is not None and entry.stale and entry.error:
                    errors.append(f"{adapter.brand}: {entry.error}, showing last known state")
        self._import_hue_rooms()
        rooms = self.store.room_map()
        local_names = self.store.device_names()
        for d in devices:
            d.room_id = rooms.get(d.id)
            d.name = local_names.get(d.id, d.name)
        devices.sort(key=lambda d: d.name.lower())
        self.last_devices = {d.id: d for d in devices}
        return devices, errors

    def _import_hue_rooms(self) -> None:
        """Put Hue lights that have no hub room into a room with their Hue room's name."""
        assigned = self.store.room_map()
        for hub_id, room_name in self.hue.room_hints.items():
            if hub_id in assigned:
                continue
            room = self.store.room_by_name(room_name) or self.store.add_room(room_name)
            self.store.set_device_room(hub_id, room["id"])

    def device_names(self) -> dict[str, str]:
        names: dict[str, str] = {}
        for adapter in self.adapters:
            names.update(adapter.names())
        return {**names, **self.store.device_names()}


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub = Hub()
    app.state.hub = hub
    for adapter in hub.adapters:
        try:
            await adapter.start()
        except Exception as exc:  # noqa: BLE001 - one brand failing must not stop the hub
            log.warning("%s did not start: %s", adapter.label, exc)
    hub.start_background()
    yield
    hub.irrigation.close()
    hub.flowtests.close()
    hub.backups.close()
    for task in (hub._collector, hub._recorder, hub._warmer):
        if task:
            task.cancel()
    hub.telemetry.stop()
    logging.getLogger().removeHandler(hub.telemetry.logs)
    for adapter in hub.adapters:
        await adapter.close()


app = FastAPI(title="Smart home hub", lifespan=lifespan)


def hub_of(request: Request) -> Hub:
    return request.app.state.hub


PUBLIC_PATHS = {
    "/api/health",
    "/api/auth/me",
    "/api/auth/google/start",
    "/api/auth/google",
    "/api/auth/passkey/login/options",
    "/api/auth/passkey/login",
    "/api/auth/logout",
    "/api/auth/app/token",
}
# Code exchange for the iPhone app: no browser, so no Origin header; the PKCE verifier protects it.
NO_ORIGIN_PATHS = {"/api/auth/app/token"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _deny(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """Web app: session cookie (Google or Face ID). Scripts: Bearer API token."""
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    hub: Hub = request.app.state.hub
    request.state.user = None

    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        bearer = authorization[7:].strip()
        if hmac.compare_digest(bearer, hub.secrets.access_token()):
            request.state.user = {"email": None, "method": "api-token"}
        elif bearer.startswith("app_") and (app_user := hub.auth.app_user(bearer)):
            request.state.user = app_user  # iPhone app, unlocked with Face ID on the phone
        else:
            return _deny(401, "Wrong API token")
    else:
        session = hub.auth.session(request.cookies.get(SESSION_COOKIE))
        # Cookie-authenticated or sign-in requests that change state must come
        # from the hub's own pages (cross-site request forgery protection).
        if request.method not in SAFE_METHODS and (session or path in PUBLIC_PATHS) and path not in NO_ORIGIN_PATHS:
            if request.headers.get("origin") not in hub.auth.origins:
                return _deny(403, "Request from an address that isn't allowed")
        request.state.user = session

    if path not in PUBLIC_PATHS and request.state.user is None:
        return _deny(401, "Sign in required")
    return await call_next(request)


@app.middleware("http")
async def observe(request: Request, call_next):
    """Outermost: times every request for the Admin tab, keeps caches honest after changes,
    and makes the browser revalidate the app's files (ETag, 304) instead of guessing."""
    t0 = time.monotonic()
    path = request.url.path
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        hub = getattr(request.app.state, "hub", None)
        if hub is not None:
            route = request.scope.get("route")
            template = getattr(route, "path", None) if route is not None else None
            if template is None:
                template = path if path.startswith("/api/") else "(static files)"
            hub.telemetry.record_request(template, status, (time.monotonic() - t0) * 1000, request.method)
            if request.method not in SAFE_METHODS and path.startswith("/api/") and status < 400:
                _after_change(hub, path)
    if not path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def _after_change(hub: "Hub", path: str) -> None:
    parts = path.split("/")
    segment = parts[2] if len(parts) > 2 else ""
    if segment == "schedules" or segment == "irrigation":
        hub.schedule_cache.invalidate_all()
    elif segment in ("devices", "rooms", "scenes", "admin", "auth"):
        pass  # handled by the route itself, or not device data
    else:
        brand = PATH_BRAND.get(segment, segment)
        if brand in hub._by_brand:
            hub.invalidate_brand(brand)


app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.exception_handler(HubError)
async def hub_error(_: Request, exc: HubError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error(_: Request, exc: sqlite3.IntegrityError):
    return JSONResponse({"detail": "A room with that name already exists"}, status_code=409)


# Health and status
@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/status")
async def status(request: Request):
    hub = hub_of(request)
    brands = await asyncio.gather(*(a.status() for a in hub.adapters))
    return {
        **{a.brand: s for a, s in zip(hub.adapters, brands)},
        "claude": {"connected": bool(hub.secrets.get("anthropic_api_key"))},
        "user": (request.state.user or {}).get("email"),
    }


# Devices
@app.get("/api/devices")
async def list_devices(request: Request, fresh: bool = False):
    hub = hub_of(request)
    devices, errors = await hub.devices(fresh=fresh)
    return {"devices": devices, "rooms": hub.store.rooms(), "errors": errors}


@app.post("/api/devices/{device_id}/state")
async def set_device_state(request: Request, device_id: str, state: StateIn):
    if state.locked is False and not (request.state.user or {}).get("email"):
        # Scripts with the API token (e.g. Siri Shortcuts) may lock, never unlock.
        raise HubError(403, "Unlocking needs you to be signed in with Google or Face ID.")
    await hub_of(request).set_state(device_id, state)
    return {"ok": True}


@app.patch("/api/devices/{device_id}")
async def patch_device(request: Request, device_id: str, patch: DevicePatch):
    hub = hub_of(request)
    if "name" in patch.model_fields_set and patch.name:
        # Names are written back to the brand, so its own app shows the same name.
        # Brands without a rename API keep the name in the hub instead.
        adapter = hub.adapter_for(device_id)
        if adapter.can_rename:
            await adapter.rename(device_id, patch.name.strip())
        else:
            hub.store.set_device_name(device_id, patch.name)
    if "room_id" in patch.model_fields_set:
        if patch.room_id is not None and hub.store.room(patch.room_id) is None:
            raise HubError(404, "Unknown room")
        hub.store.set_device_room(device_id, patch.room_id)
    return {"ok": True}


# Rooms
class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


@app.get("/api/rooms")
async def list_rooms(request: Request):
    return hub_of(request).store.rooms()


@app.post("/api/rooms")
async def add_room(request: Request, room: RoomIn):
    return hub_of(request).store.add_room(room.name)


@app.patch("/api/rooms/{room_id}")
async def rename_room(request: Request, room_id: int, room: RoomIn):
    store = hub_of(request).store
    if store.room(room_id) is None:
        raise HubError(404, "Unknown room")
    store.rename_room(room_id, room.name)
    return {"ok": True}


@app.delete("/api/rooms/{room_id}")
async def delete_room(request: Request, room_id: int):
    hub_of(request).store.delete_room(room_id)
    return {"ok": True}


@app.post("/api/rooms/{room_id}/state")
async def set_room_state(request: Request, room_id: int, state: StateIn):
    hub = hub_of(request)
    # Locks are never part of "All on / All off".
    targets = [d for d, r in hub.store.room_map().items()
               if r == room_id and not d.startswith("schlage:")]
    results = await asyncio.gather(
        *(hub.set_state(d, state) for d in targets), return_exceptions=True
    )
    failed = [d for d, r in zip(targets, results) if isinstance(r, BaseException)]
    return {"ok": not failed, "failed": failed}


# Scenes
@app.get("/api/scenes")
async def list_scenes(request: Request, fresh: bool = False):
    cache = hub_of(request).scene_cache
    return await (cache.refresh() if fresh else cache.get())


@app.post("/api/scenes/{scene_id}/activate")
async def activate_scene(request: Request, scene_id: str, dynamic: bool = Body(False, embed=True)):
    hub = hub_of(request)
    await hub.adapter_for(scene_id).activate_scene(scene_id, dynamic)
    hub.scene_cache.invalidate_all()
    hub.refresh_brand_soon(hub.adapter_for(scene_id).brand)
    return {"ok": True}


# Schedules
@app.get("/api/schedules")
async def list_schedules(request: Request, fresh: bool = False):
    hub = hub_of(request)
    cache = hub.schedule_cache
    schedules, errors = await (cache.refresh() if fresh else cache.get())
    schedules = list(schedules)
    names = hub.device_names()
    for s in schedules:
        s.device_names = [names.get(d, d) for d in s.device_ids]
    schedules.sort(key=lambda s: (s.trigger.time if s.trigger and s.trigger.time else "99", s.name))
    return {"schedules": schedules, "errors": errors}


@app.post("/api/schedules")
async def create_schedule(request: Request, spec: ScheduleIn):
    """Create the schedule natively in each brand: one rule per device."""
    hub = hub_of(request)
    if not hub.last_devices:
        await hub.devices()
    by_adapter: dict[object, list[str]] = {}
    for device_id in spec.device_ids:
        device = hub.last_devices.get(device_id)
        if device is None:
            raise HubError(404, f"Unknown device {device_id}")
        if device.kind == "lock":
            raise HubError(400, "Locks can't be scheduled.")
        # Brands with native schedules keep them on the device; everything else runs in the hub.
        native = "schedule" in device.capabilities
        by_adapter.setdefault(hub.adapter_for(device_id) if native else hub.scheduler, []).append(device_id)
    created: list[Schedule] = []
    for adapter, ids in by_adapter.items():
        created.extend(await adapter.create_schedule(spec, ids))
    return {"created": created}


class EnabledIn(BaseModel):
    enabled: bool


@app.patch("/api/schedules/{schedule_id}")
async def toggle_schedule(request: Request, schedule_id: str, body: EnabledIn):
    await hub_of(request).adapter_for(schedule_id).set_schedule_enabled(schedule_id, body.enabled)
    return {"ok": True}


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(request: Request, schedule_id: str):
    await hub_of(request).adapter_for(schedule_id).delete_schedule(schedule_id)
    return {"ok": True}


# Sign-in
def _origin(request: Request) -> str:
    return hub_of(request).auth.check_origin(request.headers.get("origin"))


def _signed_in(request: Request, email: str, method: str) -> JSONResponse:
    hub = hub_of(request)
    token = hub.auth.create_session(email, method)
    response = JSONResponse({"ok": True, "email": email})
    response.set_cookie(
        SESSION_COOKIE, token, max_age=hub.auth.session_seconds, path="/",
        httponly=True, samesite="strict", secure=_origin(request).startswith("https://"),
    )
    return response


def _email(request: Request) -> str:
    user = request.state.user or {}
    if not user.get("email"):
        raise HubError(403, "Sign in with Google or Face ID first.")
    return user["email"]


@app.get("/api/auth/me")
async def auth_me(request: Request):
    hub = hub_of(request)
    user = request.state.user
    email = (user or {}).get("email")
    return {
        "signed_in": bool(user),
        "email": email,
        "method": (user or {}).get("method"),
        "google": bool(hub.auth.config.get("google_client_id")),
        "passkeys": len(hub.auth.passkeys(email)) if email else 0,
    }


@app.post("/api/auth/google/start")
async def google_start(request: Request):
    return {"url": hub_of(request).auth.google_start(_origin(request))}


class GoogleIn(BaseModel):
    id_token: str = Field(min_length=20, max_length=8192)
    state: str = Field(min_length=8, max_length=64)


@app.post("/api/auth/google")
async def google_finish(request: Request, body: GoogleIn):
    email = await hub_of(request).auth.google_finish(_origin(request), body.id_token, body.state)
    return _signed_in(request, email, "google")


class PasskeyIn(BaseModel):
    challenge_id: str = Field(max_length=64)
    credential: dict
    label: str = Field("", max_length=40)


@app.post("/api/auth/passkey/login/options")
async def passkey_login_options(request: Request):
    return hub_of(request).auth.login_options(_origin(request))


@app.post("/api/auth/passkey/login")
async def passkey_login(request: Request, body: PasskeyIn):
    email = hub_of(request).auth.login_finish(_origin(request), body.challenge_id, body.credential)
    return _signed_in(request, email, "passkey")


@app.post("/api/auth/passkey/register/options")
async def passkey_register_options(request: Request):
    return hub_of(request).auth.register_options(_email(request), _origin(request))


@app.post("/api/auth/passkey/register")
async def passkey_register(request: Request, body: PasskeyIn):
    hub_of(request).auth.register_finish(
        _email(request), _origin(request), body.challenge_id, body.credential, body.label
    )
    return {"ok": True}


@app.get("/api/auth/passkeys")
async def list_passkeys(request: Request):
    return hub_of(request).auth.passkeys(_email(request))


@app.delete("/api/auth/passkeys/{passkey_id}")
async def delete_passkey(request: Request, passkey_id: str):
    hub_of(request).auth.delete_passkey(_email(request), passkey_id)
    return {"ok": True}


class AppCodeIn(BaseModel):
    code_challenge: str


class AppTokenIn(BaseModel):
    code: str = Field(max_length=64)
    code_verifier: str = Field(min_length=43, max_length=128)
    label: str = Field("iPhone app", max_length=60)


@app.post("/api/auth/app/code")
async def app_code(request: Request, body: AppCodeIn):
    """Signed-in web page -> one-time code for the iPhone app's smarthome:// callback."""
    code = hub_of(request).auth.app_code(_email(request), body.code_challenge)
    return {"redirect": f"{Auth.APP_REDIRECT}?code={code}"}


@app.post("/api/auth/app/token")
async def app_token(request: Request, body: AppTokenIn):
    token, email = hub_of(request).auth.app_token(body.code, body.code_verifier, body.label)
    return {"token": token, "email": email}


@app.get("/api/auth/apps")
async def list_app_tokens(request: Request):
    return hub_of(request).auth.app_tokens(_email(request))


@app.delete("/api/auth/apps/{token_id}")
async def revoke_app_token(request: Request, token_id: str):
    hub_of(request).auth.revoke_app_token(_email(request), token_id)
    return {"ok": True}


@app.post("/api/auth/logout")
async def logout(request: Request):
    hub_of(request).auth.end_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# Hue pairing
@app.post("/api/hue/pair")
async def pair_hue(request: Request):
    return await hub_of(request).hue.pair()


# LG TV pairing
class LgPairIn(BaseModel):
    host: str | None = Field(None, pattern=r"^\d{1,3}(\.\d{1,3}){3}$")


@app.post("/api/lgtv/pair")
async def pair_lgtv(request: Request, body: LgPairIn = Body(LgPairIn())):
    return await hub_of(request).lgtv.pair(body.host)


# Brand accounts. Keys are saved in ~/.smarthome/secrets.json and never sent back.
class GoveeKeyIn(BaseModel):
    api_key: str = Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9-]+$")


@app.post("/api/govee/account")
async def connect_govee(request: Request, body: GoveeKeyIn):
    return await hub_of(request).govee.connect_account(body.api_key)


@app.delete("/api/govee/account")
async def disconnect_govee(request: Request):
    await hub_of(request).govee.disconnect_account()
    return {"ok": True}


class ThinQIn(BaseModel):
    token: str = Field(min_length=20, max_length=512, pattern=r"^[A-Za-z0-9._~+/=-]+$")
    country: str = Field("US", pattern=r"^[A-Za-z]{2}$")


@app.post("/api/thinq/account")
async def connect_thinq(request: Request, body: ThinQIn):
    return await hub_of(request).thinq.connect(body.token, body.country)


@app.delete("/api/thinq/account")
async def disconnect_thinq(request: Request):
    await hub_of(request).thinq.disconnect()
    return {"ok": True}


class EmailIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    country: str = Field("US", pattern=r"^[A-Za-z]{2}$")


class CodeIn(BaseModel):
    code: str = Field(min_length=4, max_length=10, pattern=r"^\d+$")
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/dyson/code")
async def dyson_send_code(request: Request, body: EmailIn):
    return await hub_of(request).dyson.send_code(body.email, body.country)


@app.post("/api/dyson/account")
async def dyson_verify(request: Request, body: CodeIn):
    return await hub_of(request).dyson.verify_code(body.code, body.password)


class DysonStickerIn(BaseModel):
    ssid: str = Field(min_length=8, max_length=64)
    wifi_password: str = Field(min_length=6, max_length=100)
    name: str = Field("", max_length=32)


@app.post("/api/dyson/sticker")
async def dyson_sticker(request: Request, body: DysonStickerIn):
    return await hub_of(request).dyson.add_from_sticker(body.ssid, body.wifi_password, body.name)


@app.delete("/api/dyson/account")
async def dyson_disconnect(request: Request):
    await hub_of(request).dyson.disconnect()
    return {"ok": True}


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    country: str = Field("US", pattern=r"^[A-Za-z]{2}$")


@app.post("/api/ecovacs/account")
async def ecovacs_connect(request: Request, body: LoginIn):
    return await hub_of(request).ecovacs.connect(body.email, body.password, body.country)


@app.delete("/api/ecovacs/account")
async def ecovacs_disconnect(request: Request):
    await hub_of(request).ecovacs.disconnect()
    return {"ok": True}


@app.post("/api/schlage/account")
async def schlage_connect(request: Request, body: LoginIn):
    return await hub_of(request).schlage.connect(body.email, body.password)


@app.delete("/api/schlage/account")
async def schlage_disconnect(request: Request):
    await hub_of(request).schlage.disconnect()
    return {"ok": True}


class UserPassIn(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/smt/account")
async def smt_connect(request: Request, body: UserPassIn):
    return await hub_of(request).smt.connect(body.username, body.password)


@app.delete("/api/smt/account")
async def smt_disconnect(request: Request):
    await hub_of(request).smt.disconnect()
    return {"ok": True}


@app.post("/api/water/account")
async def water_connect(request: Request, body: UserPassIn):
    return await hub_of(request).water.connect(body.username, body.password)


@app.delete("/api/water/account")
async def water_disconnect(request: Request):
    await hub_of(request).water.disconnect()
    return {"ok": True}


class RainBirdIn(BaseModel):
    host: str = Field(pattern=r"^\d{1,3}(\.\d{1,3}){3}$")
    password: str = Field(min_length=1, max_length=100)


@app.post("/api/rainbird/account")
async def rainbird_connect(request: Request, body: RainBirdIn):
    return await hub_of(request).rainbird.connect(body.host, body.password)


class RainBirdTestIn(BaseModel):
    host: str | None = Field(None, pattern=r"^\d{1,3}(\.\d{1,3}){3}$")
    password: str | None = Field(None, max_length=100)


@app.post("/api/rainbird/test")
async def rainbird_test(request: Request, body: RainBirdTestIn = Body(RainBirdTestIn())):
    """Step-by-step connection test. Uses what's typed, or the saved settings. At most one controller request."""
    rb = hub_of(request).rainbird
    host = body.host or rb.config.get("host")
    if not host:
        raise HubError(400, "Enter the controller's IP address first.")
    password = body.password if body.password is not None else rb.config.get("password", "")
    return {"steps": await rb.diagnose(host, password or "")}


# Ring: email and password, then the code Ring sends
class RingLoginIn(BaseModel):
    email: str = Field("", max_length=254)
    password: str = Field("", max_length=200)
    code: str | None = Field(None, min_length=4, max_length=10, pattern=r"^\d+$")


@app.post("/api/ring/account")
async def ring_connect(request: Request, body: RingLoginIn):
    if not body.code and not (body.email and body.password):
        raise HubError(400, "Enter the Ring email and password.")
    return await hub_of(request).ring.connect(body.email, body.password, body.code)


@app.delete("/api/ring/account")
async def ring_disconnect(request: Request):
    await hub_of(request).ring.disconnect()
    return {"ok": True}


@app.get("/api/ring/{device_id}/events")
async def ring_events(request: Request, device_id: str):
    return {"events": await hub_of(request).ring.history(device_id, limit=30, fresh=True)}


@app.get("/api/ring/{device_id}/snapshot")
async def ring_snapshot(request: Request, device_id: str):
    image = await hub_of(request).ring.snapshot(device_id)
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# Nest: Device Access project + OAuth client, then link the Google account
class NestConfigIn(BaseModel):
    project_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9-]+$")
    client_id: str = Field(min_length=10, max_length=200, pattern=r"^[A-Za-z0-9._-]+$")
    client_secret: str = Field(min_length=8, max_length=200)
    subscription: str | None = Field(default=None, max_length=300,
                                     pattern=r"^(projects/[a-z][a-z0-9-]{4,29}/subscriptions/[A-Za-z][A-Za-z0-9._~%+-]{2,254})?$")


class NestCodeIn(BaseModel):
    code: str = Field(min_length=10, max_length=1000)
    state: str = Field(min_length=10, max_length=100, pattern=r"^nest\.[A-Za-z0-9_-]+$")


@app.put("/api/nest/config")
async def nest_config(request: Request, body: NestConfigIn):
    hub = hub_of(request)
    hub.nest.configure(body.project_id, body.client_id, body.client_secret, body.subscription)
    return {"url": hub.nest.link_url(_origin(request))}


@app.post("/api/nest/link")
async def nest_link(request: Request):
    return {"url": hub_of(request).nest.link_url(_origin(request))}


@app.post("/api/nest/code")
async def nest_code(request: Request, body: NestCodeIn):
    return await hub_of(request).nest.finish_link(_origin(request), body.code, body.state)


@app.get("/api/nest/events")
async def nest_events(request: Request, limit: int = 50):
    return {"events": hub_of(request).nest.recent_events(max(1, min(limit, 200)))}


@app.delete("/api/nest/account")
async def nest_disconnect(request: Request):
    await hub_of(request).nest.disconnect()
    return {"ok": True}


# Lennox iComfort: account email and password, or the thermostat's IP address for a local connection
class LennoxIn(BaseModel):
    email: str = Field("", max_length=254)
    password: str = Field("", max_length=200)
    ip: str | None = Field(None, pattern=r"^(\d{1,3}\.){3}\d{1,3}$")


@app.post("/api/lennox/account")
async def lennox_connect(request: Request, body: LennoxIn):
    return await hub_of(request).lennox.connect(body.email, body.password, body.ip)


@app.delete("/api/lennox/account")
async def lennox_disconnect(request: Request):
    await hub_of(request).lennox.disconnect()
    return {"ok": True}


@app.delete("/api/rainbird/account")
async def rainbird_disconnect(request: Request):
    await hub_of(request).rainbird.disconnect()
    return {"ok": True}


class ZoneNameIn(BaseModel):
    name: str | None = Field(None, max_length=24)
    area_sqft: float | None = Field(None, ge=0, le=100000)


@app.put("/api/rainbird/zones/{zone}")
async def rainbird_zone_name(request: Request, zone: int, body: ZoneNameIn):
    rb = hub_of(request).rainbird
    if "name" in body.model_fields_set:
        rb.name_zone(zone, body.name or "")
    if "area_sqft" in body.model_fields_set:
        rb.set_zone_area(zone, body.area_sqft)
    return {"ok": True}


HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class ZoneScheduleIn(BaseModel):
    minutes: int = Field(ge=0, le=199)
    starts: list[str] = Field(max_length=6)
    days: list[str] = []


@app.put("/api/rainbird/zones/{zone}/schedule")
async def rainbird_zone_schedule(request: Request, zone: int, body: ZoneScheduleIn):
    import re
    if any(not re.match(HHMM, s) for s in body.starts) or any(d not in DAYS for d in body.days):
        raise HubError(400, "Use HH:MM start times and day names like mon, tue.")
    return await hub_of(request).rainbird.set_zone_schedule(zone, body.minutes, body.starts, body.days)


# Smart watering programs (run by the hub)
class ZoneMinutes(BaseModel):
    zone: int = Field(ge=1, le=64)
    minutes: int = Field(ge=0, le=120)


class ProgramIn(BaseModel):
    name: str = Field("Smart program", min_length=1, max_length=32)
    starts: list[str] = Field(min_length=1, max_length=4)
    days: list[str] = Field(min_length=1)
    zones: list[ZoneMinutes] = Field(min_length=1)
    enabled: bool = True
    smart: bool = True


def _program_dict(body: ProgramIn) -> dict:
    import re
    if any(not re.match(HHMM, s) for s in body.starts) or any(d not in DAYS for d in body.days):
        raise HubError(400, "Use HH:MM start times and day names like mon, tue.")
    return body.model_dump()


@app.get("/api/irrigation/programs")
async def list_programs(request: Request):
    hub = hub_of(request)
    return {"programs": hub.irrigation.programs(), "decisions": hub.irrigation.decisions()}


@app.post("/api/irrigation/programs")
async def add_program(request: Request, body: ProgramIn):
    return hub_of(request).irrigation.save(_program_dict(body))


@app.put("/api/irrigation/programs/{program_id}")
async def edit_program(request: Request, program_id: int, body: ProgramIn):
    return hub_of(request).irrigation.save(_program_dict(body), program_id)


@app.delete("/api/irrigation/programs/{program_id}")
async def delete_program(request: Request, program_id: int):
    hub_of(request).irrigation.delete(program_id)
    return {"ok": True}


@app.post("/api/irrigation/programs/{program_id}/run")
async def run_program(request: Request, program_id: int, smart: bool = Body(True, embed=True)):
    from datetime import datetime
    hub = hub_of(request)
    program = next((p for p in hub.irrigation.programs() if p["id"] == program_id), None)
    if program is None:
        raise HubError(404, "Unknown program")
    return hub.irrigation.start_program(program, datetime.now(), smart=smart)


# Insights: water, sprinklers and weather together
class LocationIn(BaseModel):
    query: str = Field(min_length=2, max_length=80)


@app.put("/api/location")
async def set_location(request: Request, body: LocationIn):
    hub = hub_of(request)
    location = await weather_api.geocode(body.query)
    hub.secrets.set("location", location)
    report = await hub.collect_once()
    return {**location, "problems": report["problems"]}


@app.post("/api/insights/refresh")
async def refresh_insights(request: Request):
    return await hub_of(request).collect_once()


@app.post("/api/backfill")
async def backfill(request: Request):
    """Fetch the last 4 weeks of water, electricity, weather and sprinkler history now."""
    return await hub_of(request).collect_once(days=28)


@app.get("/api/energy")
async def energy(request: Request, days: int = 28):
    from datetime import date, timedelta
    hub = hub_of(request)
    days = max(7, min(days, 90))
    weather = hub.history.weather(date.today() - timedelta(days=days + 1))
    tmean = {d: w.get("tmean_f") if w.get("tmean_f") is not None else
             ((w["tmax_f"] - 10) if w.get("tmax_f") is not None else None) for d, w in weather.items()}
    return build_energy(hub.energy, tmean, days)


DEFAULT_TARIFF = {"free_start": 20, "free_end": 4, "energy_cents": None, "delivery_cents": None, "monthly_fee": None}


def tariff_of(hub) -> dict:
    """Free hours and prices: the latest imported bill, overridden by anything set by hand."""
    latest = hub.bills.latest()
    from_bill = tariff_from(latest) if latest else {}
    manual = {k: v for k, v in (hub.secrets.get("tariff") or {}).items() if v is not None}
    return {**DEFAULT_TARIFF, **from_bill, **manual}


class TariffIn(BaseModel):
    free_start: int = Field(20, ge=0, le=23)
    free_end: int = Field(4, ge=0, le=23)
    energy_cents: float | None = Field(None, ge=0, le=100)
    delivery_cents: float | None = Field(None, ge=0, le=100)
    monthly_fee: float | None = Field(None, ge=0, le=200)


@app.get("/api/bills")
async def bills(request: Request):
    hub = hub_of(request)
    return {**bills_report(hub.bills.all()), "tariff": tariff_of(hub)}


class BillImportIn(BaseModel):
    folder: str = Field("~/Downloads/TXU Invoices", max_length=300)


@app.post("/api/bills/import")
async def bills_import(request: Request, body: BillImportIn):
    """Read TXU bill PDFs from a folder in your home folder on the Mac."""
    _email(request)
    from pathlib import Path
    folder = Path(body.folder).expanduser().resolve()
    if not folder.is_dir() or Path.home().resolve() not in folder.parents:
        raise HubError(400, "Give a folder inside your home folder on the Mac, e.g. ~/Downloads/TXU Invoices.")
    return await asyncio.to_thread(import_folder, hub_of(request).bills, folder)


@app.get("/api/tariff")
async def get_tariff(request: Request):
    return tariff_of(hub_of(request))


@app.put("/api/tariff")
async def put_tariff(request: Request, body: TariffIn):
    hub_of(request).secrets.set("tariff", body.model_dump())
    return tariff_of(hub_of(request))


@app.get("/api/tou")
async def tou(request: Request, days: int = 28):
    from datetime import date, timedelta
    hub = hub_of(request)
    days = max(7, min(days, 90))
    since = date.today() - timedelta(days=days)
    intervals = {s: v for s, v in hub.energy.intervals(since).items() if s[:10] < date.today().isoformat()}
    return tou_report(intervals, tariff_of(hub)) or {"empty": True}


@app.get("/api/pool")
async def pool(request: Request, days: int = 14):
    from datetime import datetime
    hub = hub_of(request)
    pumps = hub.pentair.readings()
    pump = pumps[0] if pumps else None
    data = build_pool(hub.pool, pump["programs"] if pump else [], pump["reading"] if pump else None,
                      tariff_of(hub), datetime.now(), max(7, min(days, 60)))
    return {**data, "connected": hub.pentair.active, "pump": pump["name"] if pump else None}


class PoolLogIn(BaseModel):
    action: str
    note: str | None = Field(None, max_length=200)
    at: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


@app.post("/api/pool/maintenance")
async def pool_log(request: Request, body: PoolLogIn):
    from datetime import datetime
    if body.action not in POOL_ACTIONS:
        raise HubError(400, "Unknown maintenance action.")
    at = datetime.fromisoformat(body.at) if body.at else datetime.now()
    return hub_of(request).pool.log(body.action, at, body.note)


@app.delete("/api/pool/maintenance/{log_id}")
async def pool_log_delete(request: Request, log_id: int):
    hub_of(request).pool.delete_log(log_id)
    return {"ok": True}


def _tmean(hub, days: int) -> dict:
    from datetime import date, timedelta
    weather = hub.history.weather(date.today() - timedelta(days=days + 1))
    return {d: w.get("tmean_f") if w.get("tmean_f") is not None else
            ((w["tmax_f"] - 10) if w.get("tmax_f") is not None else None) for d, w in weather.items()}


@app.get("/api/hvac")
async def hvac(request: Request, days: int = 14):
    from datetime import date, datetime, timedelta
    hub = hub_of(request)
    days = max(7, min(days, 60))
    devices, _ = await hub.devices(interactive=False)
    stats = [{"id": d.id, "name": d.name, "brand": d.brand, "online": d.online, **(d.thermostat or {})}
             for d in devices if d.kind == "thermostat"]
    since = date.today() - timedelta(days=60)
    activity = hub.energy.activity(since)
    ids = {t["id"] for t in stats} | {a["device_id"] for a in activity if a["kind"] == "thermostat"}
    # kW per thermostat from the meter: solved with every tracked device, so the pool or dryer isn't blamed on the AC.
    kw = {d: v["kw"] for d, v in device_power(hub.energy.intervals(since), activity).items() if d in ids}
    data = build_hvac(hub.hvac, stats, tariff_of(hub), _tmean(hub, 60), kw, datetime.now(), days)
    return {**data, "connected": {"nest": hub.nest.active, "lennox": hub.lennox.active}}


class HvacLogIn(BaseModel):
    action: str
    device_id: str | None = Field(None, max_length=120)
    note: str | None = Field(None, max_length=200)
    at: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


@app.post("/api/hvac/maintenance")
async def hvac_log(request: Request, body: HvacLogIn):
    from datetime import datetime
    if body.action not in HVAC_ACTIONS:
        raise HubError(400, "Unknown maintenance action.")
    at = datetime.fromisoformat(body.at) if body.at else datetime.now()
    return hub_of(request).hvac.log(body.action, at, body.device_id, body.note)


@app.delete("/api/hvac/maintenance/{log_id}")
async def hvac_log_delete(request: Request, log_id: int):
    hub_of(request).hvac.delete_log(log_id)
    return {"ok": True}


@app.get("/api/insights")
async def insights(request: Request, days: int = 28):
    hub = hub_of(request)
    days = max(7, min(days, 90))
    data = build_insights(hub.history, hub.rainbird.zone_label, hub.rainbird.zone_areas(), days,
                          measured=hub.flowtests.measured_gpm(), photo_advice=hub.photo_advice,
                          water_diag=hub.water.diagnostics, watch_error=hub.rainbird.watch_error,
                          tz_name=(hub.secrets.get("location") or {}).get("timezone", "America/Chicago"))
    return {**data, "location": hub.secrets.get("location")}


# Zone photos
@app.post("/api/rainbird/zones/{zone}/photos")
async def add_zone_photo(request: Request, zone: int):
    """Body: the photo itself (image/jpeg, image/png or image/heic)."""
    from datetime import datetime
    hub = hub_of(request)
    if not (request.headers.get("content-type") or "").startswith("image/"):
        raise HubError(415, "Send the photo as an image.")
    body = await request.body()
    photo = await hub.photos.add(zone, body, datetime.now())
    key = hub.secrets.get("anthropic_api_key")
    if key:
        try:
            ai = await asyncio.to_thread(assess_with_claude, key, body, hub.claude_context(zone))
            hub.photos.set_ai(photo["id"], ai)
            photo = hub.photos.get(photo["id"])
        except HubError as exc:
            photo["ai_error"] = exc.message
    return {"photo": photo, "advice": hub.photo_advice(zone, None, None, None)}


@app.get("/api/rainbird/zones/{zone}/photos")
async def zone_photos(request: Request, zone: int):
    return hub_of(request).photos.for_zone(zone)


@app.get("/api/photos/{photo_id}")
async def photo_file(request: Request, photo_id: int):
    from fastapi.responses import FileResponse
    return FileResponse(hub_of(request).photos.path(photo_id), media_type="image/jpeg")


@app.delete("/api/photos/{photo_id}")
async def delete_photo(request: Request, photo_id: int):
    hub_of(request).photos.delete(photo_id)
    return {"ok": True}


@app.post("/api/photos/{photo_id}/assess")
async def reassess_photo(request: Request, photo_id: int):
    hub = hub_of(request)
    key = hub.secrets.get("anthropic_api_key")
    if not key:
        raise HubError(409, "Add an Anthropic API key in Settings → Photo analysis first.")
    photo = hub.photos.get(photo_id)
    ai = await asyncio.to_thread(assess_with_claude, key, hub.photos.path(photo_id).read_bytes(),
                                 hub.claude_context(photo["zone"]))
    hub.photos.set_ai(photo_id, ai)
    return hub.photos.get(photo_id)


class ApiKeyIn(BaseModel):
    api_key: str = Field(min_length=20, max_length=300, pattern=r"^sk-ant-[A-Za-z0-9_-]+$")


@app.put("/api/settings/claude")
async def set_claude_key(request: Request, body: ApiKeyIn):
    hub_of(request).secrets.set("anthropic_api_key", body.api_key)
    return {"ok": True}


@app.delete("/api/settings/claude")
async def clear_claude_key(request: Request):
    hub_of(request).secrets.set("anthropic_api_key", None)
    return {"ok": True}


# Flow tests
class FlowTestIn(BaseModel):
    zones: list[int] = Field(min_length=1, max_length=24)
    minutes: int = Field(10, ge=3, le=30)


@app.get("/api/rainbird/flow-tests")
async def list_flow_tests(request: Request):
    hub = hub_of(request)
    return {"tests": hub.flowtests.tests(), "measured": hub.flowtests.measured_gpm()}


@app.post("/api/rainbird/flow-tests")
async def plan_flow_tests(request: Request, body: FlowTestIn):
    from datetime import datetime
    return hub_of(request).flowtests.schedule(body.zones, body.minutes, datetime.now())


@app.delete("/api/rainbird/flow-tests")
async def cancel_flow_tests(request: Request):
    hub_of(request).flowtests.cancel()
    return {"ok": True}


# What is stored, and where (names and counts only, never values)
STORED_SETTINGS = {
    "access_token": ("Hub API token (for scripts)", "token"),
    "auth": ("Google sign-in settings (client ID, allowed emails, addresses)", "config"),
    "location": ("Weather location (city only)", "config"),
    "hue": ("Hue Bridge key", "key"),
    "lgtv": ("LG TV pairing key", "key"),
    "govee_api_key": ("Govee API key", "key"),
    "thinq": ("LG ThinQ token", "key"),
    "dyson": ("Dyson local device keys", "key"),
    "ecovacs": ("Ecovacs login (password hash)", "hash"),
    "pentair": ("Pentair login tokens", "key"),
    "schlage": ("Schlage email and password", "password"),
    "smt": ("Smart Meter Texas username and password", "password"),
    "water": ("My Water Advisor email and password", "password"),
    "rainbird": ("Rain Bird controller address and password, zone names and sizes", "password"),
    "ring": ("Ring sign-in token (not the password)", "key"),
    "lennox": ("Lennox iComfort email and password (or thermostat address)", "password"),
    "nest": ("Nest (Google Device Access) client secret and refresh token", "key"),
    "anthropic_api_key": ("Anthropic API key (photo analysis)", "key"),
    "s3_backup": ("S3 backup bucket and AWS access key", "password"),
    "tariff": ("Electricity plan: free hours and prices from your bill", "config"),
}
DATA_GROUPS = {
    "Rooms and names": ["rooms", "device_rooms", "device_names"],
    "Sign-in": ["sessions", "passkeys", "app_tokens"],
    "Schedules and programs": ["hub_schedules", "irrigation_programs", "irrigation_decisions", "flow_tests"],
    "Water, sprinklers and weather history": ["watering", "water_hourly", "water_daily", "weather"],
    "Electricity history": ["energy_interval", "device_activity"],
    "Pool pump history": ["pool_samples", "pool_maintenance"],
    "Heating and cooling history": ["hvac_samples", "hvac_maintenance"],
    "Electricity bills": ["bills"],
    "Zone photos": ["zone_photos"],
    "Backup log": ["backups"],
}


def _size(path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@app.get("/api/storage")
async def storage(request: Request):
    hub = hub_of(request)
    folder = data_dir()
    tables = {r[0] for r in hub.store.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    counts = {t: hub.store.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    photos = list((folder / "photos").glob("*.jpg")) if (folder / "photos").exists() else []
    return {
        "folder": str(folder),
        "settings_file": {"name": "secrets.json", "bytes": _size(folder / "secrets.json"),
                          "backup": (folder / "secrets.json.bak").exists()},
        "database": {"name": "hub.db", "bytes": _size(folder / "hub.db") + _size(folder / "hub.db-wal"), "wal": True},
        "photos": {"count": len(photos), "bytes": sum(_size(f) for f in photos)},
        "saved": [{"key": k, "label": STORED_SETTINGS.get(k, (k, "config"))[0], "kind": STORED_SETTINGS.get(k, (k, "config"))[1]}
                  for k in hub.secrets.keys()],
        "data": [{"group": g, "rows": sum(counts.get(t, 0) for t in ts)} for g, ts in DATA_GROUPS.items()],
    }


# Encrypted backups to the user's S3 bucket
class BackupIn(BaseModel):
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{2,62}$")
    region: str = Field("us-east-1", pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    prefix: str = Field("smarthome", pattern=r"^[A-Za-z0-9/_-]{0,100}$")
    access_key_id: str = Field("", max_length=128, pattern=r"^[A-Z0-9]*$")
    secret_access_key: str = Field("", max_length=128, pattern=r"^[A-Za-z0-9/+=]*$")
    include_photos: bool = True


@app.get("/api/backup")
async def backup_status(request: Request):
    b = hub_of(request).backups
    cfg = b.config
    history = b.history()
    return {
        "configured": b.active, "bucket": cfg.get("bucket"), "region": cfg.get("region"), "prefix": cfg.get("prefix"),
        "include_photos": cfg.get("include_photos", True), "own_keys": bool(cfg.get("access_key_id")),
        "last_ok": next((h for h in history if h["ok"]), None), "last": history[0] if history else None,
        "history": history, "runs_at": "3:30 AM",
    }


@app.put("/api/backup")
async def backup_connect(request: Request, body: BackupIn):
    if bool(body.access_key_id) != bool(body.secret_access_key):
        raise HubError(400, "Enter both the access key ID and the secret, or neither to use the Mac's AWS login.")
    cfg = {**body.model_dump(), "prefix": body.prefix.strip("/")}
    return await hub_of(request).backups.connect(cfg)


@app.post("/api/backup/run")
async def backup_now(request: Request):
    b = hub_of(request).backups
    if not b.active:
        raise HubError(409, "Set up the S3 bucket first.")
    return await b.run()


@app.delete("/api/backup")
async def backup_off(request: Request):
    hub_of(request).backups.disconnect()
    return {"ok": True}


@app.get("/api/backup/key")
async def backup_key(request: Request):
    """The recovery key. Only a person signed in with Google or Face ID, never the script token."""
    _email(request)
    return {"key": hub_of(request).backups.recovery_key()}


@app.get("/api/pentair/fields")
async def pentair_fields(request: Request):
    return hub_of(request).pentair.raw_fields()


@app.post("/api/pentair/account")
async def pentair_connect(request: Request, body: LoginIn):
    return await hub_of(request).pentair.connect(body.email, body.password)


@app.delete("/api/pentair/account")
async def pentair_disconnect(request: Request):
    await hub_of(request).pentair.disconnect()
    return {"ok": True}


# Admin: telemetry and manual refresh
@app.get("/api/admin/telemetry")
async def admin_telemetry(request: Request):
    hub = hub_of(request)
    t = hub.telemetry
    brands = []
    statuses = await asyncio.gather(*(a.status() for a in hub.adapters), return_exceptions=True)
    ops = t.ops_report()
    for a, st in zip(hub.adapters, statuses):
        entry = hub.device_cache.entry(a.brand)
        brands.append({
            "brand": a.brand, "label": a.label, "active": a.active,
            "status": st if isinstance(st, dict) else {"error": str(st)},
            "cache": None if entry is None else {"age_s": round(hub.device_cache.age(a.brand), 1),
                                                 "stale": entry.stale, "error": entry.error, "devices": len(entry.value)},
            "ops": ops.get(a.brand, {}),
        })
    jobs = dict(t.jobs)
    try:
        if hub.backups.active:
            last = (hub.backups.history(1) or [None])[0]
            if last:
                jobs["nightly backup"] = {"runs": None, "ok": bool(last.get("ok")), "last_at": last.get("at"),
                                          "detail": last.get("error") or last.get("key") or None}
    except Exception:  # noqa: BLE001
        pass
    return {
        "now": time.time(), "process": t.process(), "host": t.host(), "loop": t.loop(),
        "requests": {"per_min": t.requests_per_minute(), "routes": t.routes_report(), "slow": list(t.slow)[::-1]},
        "brands": brands, "caches": [c.snapshot() for c in hub.caches.values()],
        "jobs": jobs, "series": list(t.series), "activity": list(t.activity),
        "logs": t.log_report("WARNING", 50),
    }


@app.get("/api/admin/logs")
async def admin_logs(request: Request, level: str = "WARNING", limit: int = 200):
    return hub_of(request).telemetry.log_report(level, max(1, min(limit, 200)))


class RawRefreshIn(BaseModel):
    brands: list[str] | None = None
    collect: bool = False
    backfill_days: int | None = Field(None, ge=1, le=28)


@app.post("/api/admin/refresh/raw")
async def admin_refresh_raw(request: Request, body: RawRefreshIn = Body(RawRefreshIn())):
    who = _email(request)
    hub = hub_of(request)
    t0 = time.monotonic()
    result = await hub.raw_refresh(body.brands, body.collect, body.backfill_days)
    ms = (time.monotonic() - t0) * 1000
    bad = [b for b, r in result["brands"].items() if not r["ok"]]
    what = "Raw refresh: " + (", ".join(body.brands) if body.brands else "everything") + \
        (f" + {body.backfill_days}-day history" if body.backfill_days else " + history" if body.collect else "")
    summary = f"{len(result['brands']) - len(bad)} ok" + (f", failed: {', '.join(bad)}" if bad else "")
    if "history" in result:
        summary += "; history " + ("ok" if result["history"]["ok"] else "had problems")
    hub.telemetry.add_activity(who, what, ms, summary, ok=not bad and result.get("history", {}).get("ok", True))
    return {**result, "ms": round(ms)}


class CacheRefreshIn(BaseModel):
    cache: str | None = None
    key: str | None = None
    reload: bool = True


@app.post("/api/admin/cache/refresh")
async def admin_cache_refresh(request: Request, body: CacheRefreshIn = Body(CacheRefreshIn())):
    who = _email(request)
    hub = hub_of(request)
    if body.cache and body.cache not in hub.caches:
        raise HubError(404, f"Unknown cache {body.cache}")
    targets = [hub.caches[body.cache]] if body.cache else list(hub.caches.values())
    t0 = time.monotonic()
    out = {}
    for c in targets:
        before = {"size": len(c), "hit_rate": c.snapshot()["hit_rate"]}
        if body.key is not None:
            c.invalidate(body.key)
        else:
            c.invalidate_all()
        errors = []
        if body.reload:
            keys = ([body.key] if body.key is not None else [a.brand for a in hub.active_adapters()]) \
                if c is hub.device_cache else [None]
            results = await asyncio.gather(*(c.refresh(k, timeout=RAW_TIMEOUT) for k in keys), return_exceptions=True)
            errors = [f"{k}: {'no answer in time' if isinstance(r, asyncio.TimeoutError) else r}"
                      for k, r in zip(keys, results) if isinstance(r, BaseException)]
        out[c.name] = {"before": before, "after": {"size": len(c)}, "errors": errors}
    ms = (time.monotonic() - t0) * 1000
    what = ("Reload" if body.reload else "Clear") + " cache: " + (body.cache or "all") + (f" [{body.key}]" if body.key else "")
    errs = [e for v in out.values() for e in v["errors"]]
    hub.telemetry.add_activity(who, what, ms, "ok" if not errs else "; ".join(errs)[:200], ok=not errs)
    return {"caches": out, "ms": round(ms)}


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
