"""HTTP API and web app for the hub."""

from __future__ import annotations

import asyncio
import hmac
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .adapters.base import Adapter
from .adapters.dyson_adapter import DysonAdapter
from .adapters.ecovacs_adapter import EcovacsAdapter
from .adapters.govee_adapter import GoveeAdapter
from .adapters.hue_adapter import HueAdapter
from .adapters.kasa_adapter import KasaAdapter
from .adapters.lgtv_adapter import LgTvAdapter
from .adapters.pentair_adapter import PentairAdapter
from .adapters.rainbird_adapter import RainBirdAdapter
from .adapters.schlage_adapter import SchlageAdapter
from .adapters.smt_adapter import SmartMeterTexasAdapter
from .adapters.sonos_adapter import SonosAdapter
from .adapters.thinq_adapter import ThinQAdapter
from .adapters.water_adapter import WaterAdvisorAdapter
from .auth import SESSION_COOKIE, Auth
from .config import Secrets, data_dir
from .scheduler import HubScheduler
from .models import Device, DevicePatch, HubError, Schedule, ScheduleIn, StateIn

log = logging.getLogger("hub")
WEB_DIR = Path(__file__).parent / "web"


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
        self.adapters: list[Adapter] = [self.kasa, self.hue, self.sonos, self.govee, self.lgtv, self.thinq,
                                        self.dyson, self.ecovacs, self.pentair, self.schlage, self.smt, self.water,
                                        self.rainbird]
        self.scheduler = HubScheduler(self.store.db, self._apply)
        self.adapters.append(self.scheduler)
        self.last_devices: dict[str, Device] = {}
        self._by_prefix = {p: a for a in self.adapters for p in a.prefixes}

    async def _apply(self, device_id: str, state: StateIn) -> None:
        await self.adapter_for(device_id).set_state(device_id, state)

    def adapter_for(self, item_id: str) -> Adapter:
        adapter = self._by_prefix.get(item_id.split(":", 1)[0])
        if adapter is None:
            raise HubError(404, f"Unknown item {item_id}")
        return adapter

    def active_adapters(self) -> list[Adapter]:
        return [a for a in self.adapters if a.active]

    async def devices(self) -> tuple[list[Device], list[str]]:
        adapters = self.active_adapters()
        results = await asyncio.gather(*(a.devices() for a in adapters), return_exceptions=True)
        devices: list[Device] = []
        errors: list[str] = []
        for adapter, result in zip(adapters, results):
            if isinstance(result, BaseException):
                log.warning("%s: %s", adapter.brand, result)
                errors.append(f"{adapter.brand}: {getattr(result, 'message', result)}")
            else:
                devices.extend(result)
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
    yield
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
}
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
        if not hmac.compare_digest(authorization[7:].strip(), hub.secrets.access_token()):
            return _deny(401, "Wrong API token")
        request.state.user = {"email": None, "method": "api-token"}
    else:
        session = hub.auth.session(request.cookies.get(SESSION_COOKIE))
        # Cookie-authenticated or sign-in requests that change state must come
        # from the hub's own pages (cross-site request forgery protection).
        if request.method not in SAFE_METHODS and (session or path in PUBLIC_PATHS):
            if request.headers.get("origin") not in hub.auth.origins:
                return _deny(403, "Request from an address that isn't allowed")
        request.state.user = session

    if path not in PUBLIC_PATHS and request.state.user is None:
        return _deny(401, "Sign in required")
    return await call_next(request)


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
        "user": (request.state.user or {}).get("email"),
    }


# Devices
@app.get("/api/devices")
async def list_devices(request: Request):
    hub = hub_of(request)
    devices, errors = await hub.devices()
    return {"devices": devices, "rooms": hub.store.rooms(), "errors": errors}


@app.post("/api/devices/{device_id}/state")
async def set_device_state(request: Request, device_id: str, state: StateIn):
    if state.locked is False and not (request.state.user or {}).get("email"):
        # Scripts with the API token (e.g. Siri Shortcuts) may lock, never unlock.
        raise HubError(403, "Unlocking needs you to be signed in with Google or Face ID.")
    await hub_of(request).adapter_for(device_id).set_state(device_id, state)
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
        *(hub.adapter_for(d).set_state(d, state) for d in targets), return_exceptions=True
    )
    failed = [d for d, r in zip(targets, results) if isinstance(r, BaseException)]
    return {"ok": not failed, "failed": failed}


# Scenes
@app.get("/api/scenes")
async def list_scenes(request: Request):
    hub = hub_of(request)
    results = await asyncio.gather(*(a.scenes() for a in hub.active_adapters()))
    return [scene for scenes in results for scene in scenes]


@app.post("/api/scenes/{scene_id}/activate")
async def activate_scene(request: Request, scene_id: str, dynamic: bool = Body(False, embed=True)):
    await hub_of(request).adapter_for(scene_id).activate_scene(scene_id, dynamic)
    return {"ok": True}


# Schedules
@app.get("/api/schedules")
async def list_schedules(request: Request):
    hub = hub_of(request)
    adapters = hub.active_adapters()
    results = await asyncio.gather(*(a.schedules() for a in adapters), return_exceptions=True)
    schedules: list[Schedule] = []
    errors: list[str] = []
    for adapter, result in zip(adapters, results):
        if isinstance(result, BaseException):
            errors.append(f"{adapter.brand}: {getattr(result, 'message', result)}")
        else:
            schedules.extend(result)
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


@app.delete("/api/rainbird/account")
async def rainbird_disconnect(request: Request):
    await hub_of(request).rainbird.disconnect()
    return {"ok": True}


class ZoneNameIn(BaseModel):
    name: str = Field("", max_length=24)


@app.put("/api/rainbird/zones/{zone}")
async def rainbird_zone_name(request: Request, zone: int, body: ZoneNameIn):
    hub_of(request).rainbird.name_zone(zone, body.name)
    return {"ok": True}


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


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
