"""Defaults for adapters, so a brand only implements what it supports."""

from __future__ import annotations

from ..models import Device, HubError, Scene, Schedule, ScheduleIn, StateIn


class Adapter:
    brand = ""
    label = ""
    prefixes: tuple[str, ...] = ()
    # False: the brand has no rename API, so the hub keeps the name itself.
    can_rename = True

    @property
    def active(self) -> bool:
        return True

    async def start(self) -> None:
        """Called once when the hub starts (background discovery, sockets)."""

    async def devices(self) -> list[Device]:
        return []

    def names(self) -> dict[str, str]:
        return {}

    async def set_state(self, device_id: str, state: StateIn) -> None:
        raise HubError(400, f"{self.label} devices can't do that")

    async def rename(self, device_id: str, name: str) -> None:
        raise HubError(400, f"{self.label} devices can't be renamed from the hub")

    async def scenes(self) -> list[Scene]:
        return []

    async def activate_scene(self, scene_id: str, dynamic: bool) -> None:
        raise HubError(404, "Unknown scene")

    async def schedules(self) -> list[Schedule]:
        return []

    async def create_schedule(self, spec: ScheduleIn, device_ids: list[str]) -> list[Schedule]:
        raise HubError(400, f"{self.label} devices don't support schedules yet")

    async def delete_schedule(self, schedule_id: str) -> None:
        raise HubError(404, "Schedule not found")

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        raise HubError(404, "Schedule not found")

    async def status(self) -> dict:
        return {"devices": len(self.names())}

    async def close(self) -> None:
        """Release sockets and connections."""
