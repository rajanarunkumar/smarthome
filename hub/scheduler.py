"""Schedules that run in the hub itself, for devices that can't store their own
(Pentair pump, Govee, Dyson, Sonos, LG TV, Ecovacs…). Kasa and Hue keep using
their native schedules, which run even when the Mac is off; these run only
while the hub is running, so keep the Mac awake (System Settings → Energy →
"Prevent automatic sleeping") if you rely on them.

Locks are never scheduled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable

from .adapters.base import Adapter
from .models import DAYS, Action, HubError, Schedule, ScheduleIn, StateIn, Trigger

log = logging.getLogger(__name__)
CHECK_SECONDS = 20


class HubScheduler(Adapter):
    brand = "hub"
    label = "Hub"
    prefixes = ("hubsched",)

    def __init__(self, db, apply: Callable[[str, StateIn], Any]) -> None:
        self.db = db
        self.apply = apply  # (device id, state) -> awaitable, provided by the Hub
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS hub_schedules (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, device_ids TEXT NOT NULL,
                action TEXT NOT NULL, time TEXT NOT NULL, days TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, last_run TEXT)"""
        )
        self.db.commit()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due(datetime.now())
            except Exception as exc:  # noqa: BLE001 - the loop must keep going
                log.warning("Hub schedules: %s", exc)
            await asyncio.sleep(CHECK_SECONDS)

    def _rows(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM hub_schedules ORDER BY time")]

    async def run_due(self, now: datetime) -> list[int]:
        """Run every enabled schedule whose minute is now and that hasn't run this minute."""
        stamp, hhmm, day = now.strftime("%Y-%m-%d %H:%M"), now.strftime("%H:%M"), DAYS[now.weekday()]
        ran = []
        for row in self._rows():
            if not row["enabled"] or row["time"] != hhmm or day not in json.loads(row["days"]) \
                    or row["last_run"] == stamp:
                continue
            self.db.execute("UPDATE hub_schedules SET last_run = ? WHERE id = ?", (stamp, row["id"]))
            self.db.commit()
            action = Action(**json.loads(row["action"]))
            state = StateIn(on=action.on, brightness=action.brightness if action.on else None)
            for device_id in json.loads(row["device_ids"]):
                try:
                    await self.apply(device_id, state)
                except Exception as exc:  # noqa: BLE001 - one device failing must not stop the others
                    log.warning("Schedule %s: %s failed: %s", row["name"], device_id, exc)
            ran.append(row["id"])
        return ran

    def _to_schedule(self, row: dict) -> Schedule:
        return Schedule(
            id=f"hubsched:{row['id']}", brand="hub", runs_on="Hub (this Mac)", origin="hub",
            name=row["name"], device_ids=json.loads(row["device_ids"]),
            action=Action(**json.loads(row["action"])), trigger=Trigger(type="time", time=row["time"]),
            days=json.loads(row["days"]), enabled=bool(row["enabled"]),
        )

    async def schedules(self) -> list[Schedule]:
        return [self._to_schedule(r) for r in self._rows()]

    async def create_schedule(self, spec: ScheduleIn, device_ids: list[str]) -> list[Schedule]:
        if spec.trigger.type != "time":
            raise HubError(400, "Sunrise and sunset schedules work only for Kasa and Hue. Pick a time instead.")
        if any(d.startswith("schlage:") for d in device_ids):
            raise HubError(400, "Locks can't be scheduled.")
        cur = self.db.execute(
            "INSERT INTO hub_schedules (name, device_ids, action, time, days, enabled) VALUES (?, ?, ?, ?, ?, ?)",
            (spec.name, json.dumps(device_ids), spec.action.model_dump_json(), spec.trigger.time,
             json.dumps(spec.days), int(spec.enabled)),
        )
        self.db.commit()
        row = dict(self.db.execute("SELECT * FROM hub_schedules WHERE id = ?", (cur.lastrowid,)).fetchone())
        return [self._to_schedule(row)]

    def _id(self, schedule_id: str) -> int:
        try:
            return int(schedule_id.split(":", 1)[1])
        except (IndexError, ValueError):
            raise HubError(404, "Schedule not found") from None

    async def delete_schedule(self, schedule_id: str) -> None:
        self.db.execute("DELETE FROM hub_schedules WHERE id = ?", (self._id(schedule_id),))
        self.db.commit()

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        self.db.execute("UPDATE hub_schedules SET enabled = ? WHERE id = ?", (int(enabled), self._id(schedule_id)))
        self.db.commit()

    async def status(self) -> dict:
        return {"schedules": len(self._rows())}

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
