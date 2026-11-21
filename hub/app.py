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

from .adapters.hue_adapter import HueAdapter
from .adapters.kasa_adapter import KasaAdapter
from .config import Secrets, data_dir
from .models import Device, DevicePatch, HubError, Schedule, ScheduleIn, StateIn

log = logging.getLogger("hub")
WEB_DIR = Path(__file__).parent / "web"


class Hub:
    def __init__(self) -> None:
        from .store import Store

        self.secrets = Secrets()
        self.store = Store(data_dir() / "hub.db")
        self.kasa = KasaAdapter()
        self.hue = HueAdapter(self.secrets)

    def adapter_for(self, item_id: str):
        prefix = item_id.split(":", 1)[0]
        if prefix == "kasa":
            return self.kasa
        if prefix in {"hue", "huev1", "hueapp"}:
            return self.hue
        raise HubError(404, f"Unknown item {item_id}")

    def active_adapters(self):
        return [self.kasa] + ([self.hue] if self.hue.paired else [])

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
        for d in devices:
            d.room_id = rooms.get(d.id)
        devices.sort(key=lambda d: d.name.lower())
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
        return {**self.kasa.names(), **self.hue.names()}


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub = Hub()
    app.state.hub = hub
    asyncio.create_task(hub.kasa.discover())
    yield
    await hub.kasa.close()
    await hub.hue.close()


app = FastAPI(title="Smart home hub", lifespan=lifespan)


def hub_of(request: Request) -> Hub:
    return request.app.state.hub


@app.middleware("http")
async def require_token(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        expected = request.app.state.hub.secrets.access_token()
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, expected):
            return JSONResponse({"detail": "Missing or wrong access token"}, status_code=401)
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
    return {"hue": await hub.hue.status(), "kasa": {"devices": len(hub.kasa.names())}}


# Devices
@app.get("/api/devices")
async def list_devices(request: Request):
    hub = hub_of(request)
    devices, errors = await hub.devices()
    return {"devices": devices, "rooms": hub.store.rooms(), "errors": errors}


@app.post("/api/devices/{device_id}/state")
async def set_device_state(request: Request, device_id: str, state: StateIn):
    await hub_of(request).adapter_for(device_id).set_state(device_id, state)
    return {"ok": True}


@app.patch("/api/devices/{device_id}")
async def patch_device(request: Request, device_id: str, patch: DevicePatch):
    hub = hub_of(request)
    if "name" in patch.model_fields_set and patch.name:
        # Names are written back to the brand, so its own app shows the same name.
        await hub.adapter_for(device_id).rename(device_id, patch.name.strip())
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
    targets = [d for d, r in hub.store.room_map().items() if r == room_id]
    results = await asyncio.gather(
        *(hub.adapter_for(d).set_state(d, state) for d in targets), return_exceptions=True
    )
    failed = [d for d, r in zip(targets, results) if isinstance(r, BaseException)]
    return {"ok": not failed, "failed": failed}


# Scenes
@app.get("/api/scenes")
async def list_scenes(request: Request):
    hub = hub_of(request)
    return await hub.hue.scenes() if hub.hue.paired else []


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
    by_adapter: dict[object, list[str]] = {}
    for device_id in spec.device_ids:
        by_adapter.setdefault(hub.adapter_for(device_id), []).append(device_id)
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


# Hue pairing
@app.post("/api/hue/pair")
async def pair_hue(request: Request):
    return await hub_of(request).hue.pair()


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
