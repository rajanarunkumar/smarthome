"""Brand-neutral data shapes shared by every adapter and the web app."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS: list[str] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HEX = r"^#[0-9a-fA-F]{6}$"


class HubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Device(BaseModel):
    id: str
    brand: str
    name: str
    model: str | None = None
    kind: Literal["plug", "dimmer", "light", "speaker", "tv", "appliance"]
    room_id: int | None = None
    online: bool = True
    on: bool | None = None
    brightness: int | None = None
    color: str | None = None
    mirek: int | None = None
    mirek_min: int | None = None
    mirek_max: int | None = None
    # Speakers and TVs
    volume: int | None = None
    muted: bool | None = None
    playing: bool | None = None
    media: str | None = None           # what's playing, the TV's app, or an appliance's status
    source: str | None = None          # current input / app id
    sources: list[dict[str, str]] = []  # [{"id": ..., "label": ...}]
    capabilities: list[str] = []


class StateIn(BaseModel):
    on: bool | None = None
    brightness: int | None = Field(None, ge=1, le=100)
    color: str | None = Field(None, pattern=HEX)
    mirek: int | None = Field(None, ge=50, le=1000)
    volume: int | None = Field(None, ge=0, le=100)
    muted: bool | None = None
    playback: Literal["play", "pause", "next", "previous"] | None = None
    source: str | None = Field(None, min_length=1, max_length=128)


class DevicePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=32)
    room_id: int | None = None


class Action(BaseModel):
    on: bool
    brightness: int | None = Field(None, ge=1, le=100)


class Trigger(BaseModel):
    type: Literal["time", "sunrise", "sunset"] = "time"
    time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    offset_min: int = Field(0, ge=-180, le=180)

    @model_validator(mode="after")
    def _time_required(self) -> Trigger:
        if self.type == "time" and not self.time:
            raise ValueError("time is required for a time trigger")
        return self


class ScheduleIn(BaseModel):
    name: str = Field("Schedule", min_length=1, max_length=32)
    device_ids: list[str] = Field(min_length=1)
    action: Action
    trigger: Trigger
    days: list[Day] = Field(min_length=1)
    enabled: bool = True


class Schedule(BaseModel):
    id: str
    brand: str
    runs_on: str
    origin: Literal["hub", "app"]
    name: str
    device_ids: list[str] = []
    device_names: list[str] = []
    action: Action | None = None
    trigger: Trigger | None = None
    days: list[str] = []
    enabled: bool = True
    can_delete: bool = True
    can_toggle: bool = True
    summary: str | None = None


class Scene(BaseModel):
    id: str
    brand: str
    name: str
    group: str | None = None
    active: bool = False
    dynamic: bool = False
