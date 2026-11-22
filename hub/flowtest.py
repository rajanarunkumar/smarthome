"""Flow tests: measure each zone's gallons per minute directly.

The house uses almost no water between midnight and 6 AM. A flow test runs
each zone by itself for a few minutes in its own clock hour (12:05, 1:05,
2:05…), up to 6 zones a night; leftover zones run the next night. Once My
Water Advisor posts that hour, the hub takes the hour's gallons minus the
usual overnight use for that hour (median of recent nights with no
watering) and divides by the minutes run.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import date, datetime, timedelta

from .models import HubError

log = logging.getLogger(__name__)
CHECK_SECONDS = 20
ZONES_PER_NIGHT = 6
GIVE_UP_AFTER = timedelta(days=3)


def plan(zones: list[int], minutes: int, first_night: date) -> list[dict]:
    """[{zone, start}] one zone per hour from 00:05, six per night."""
    out = []
    for i, zone in enumerate(zones):
        night = first_night + timedelta(days=i // ZONES_PER_NIGHT)
        start = datetime.combine(night, datetime.min.time()) + timedelta(hours=i % ZONES_PER_NIGHT, minutes=5)
        out.append({"zone": zone, "start": start.isoformat(timespec="minutes"), "minutes": minutes})
    return out


def overnight_baseline(hourly: dict[str, float], hour: int, busy: set[str], before: str, nights: int = 14) -> float | None:
    """Median gallons in this clock hour over recent nights with no watering."""
    values = [v for h, v in hourly.items() if int(h[11:13]) == hour and h < before and h not in busy]
    values = sorted(values)[-nights:] if len(values) > nights else values
    return statistics.median(values) if len(values) >= 3 else None


def measure(test: dict, hourly: dict[str, float], busy: set[str]) -> dict | None:
    """{"gallons", "gpm", "baseline"} once the hour is posted; None while waiting."""
    hour_key = test["start"][:13]
    if hour_key not in hourly:
        return None
    baseline = overnight_baseline(hourly, int(hour_key[11:13]), busy, hour_key) or 0.0
    gallons = max(hourly[hour_key] - baseline, 0.0)
    return {"gallons": round(gallons, 1), "gpm": round(gallons / test["minutes"], 2), "baseline": round(baseline, 1)}


class FlowTests:
    def __init__(self, db, rainbird) -> None:
        self.db, self.rainbird = db, rainbird
        db.execute("""CREATE TABLE IF NOT EXISTS flow_tests (
            id INTEGER PRIMARY KEY, zone INTEGER NOT NULL, start TEXT NOT NULL, minutes INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'planned', gallons REAL, gpm REAL, note TEXT)""")
        db.commit()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due(datetime.now())
            except Exception as exc:  # noqa: BLE001
                log.warning("Flow tests: %s", exc)
            await asyncio.sleep(CHECK_SECONDS)

    def tests(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM flow_tests ORDER BY start DESC LIMIT 60")]

    def schedule(self, zones: list[int], minutes: int, now: datetime) -> list[dict]:
        if not zones:
            raise HubError(400, "Pick at least one zone.")
        if any(t["status"] == "planned" for t in self.tests()):
            raise HubError(409, "A flow test is already planned. Cancel it first.")
        first_night = now.date() + timedelta(days=1)  # tonight after midnight
        rows = plan(zones, minutes, first_night)
        self.db.executemany("INSERT INTO flow_tests (zone, start, minutes) VALUES (:zone, :start, :minutes)", rows)
        self.db.commit()
        return [t for t in self.tests() if t["status"] == "planned"]

    def cancel(self) -> None:
        self.db.execute("DELETE FROM flow_tests WHERE status = 'planned'")
        self.db.commit()

    async def run_due(self, now: datetime) -> list[int]:
        due = [dict(r) for r in self.db.execute(
            "SELECT * FROM flow_tests WHERE status = 'planned' AND start <= ?", (now.isoformat(timespec="minutes"),))]
        started = []
        for t in due:
            late = now - datetime.fromisoformat(t["start"]) > timedelta(minutes=10)
            if late:  # the Mac was asleep: don't run it in the morning when the house uses water
                self.db.execute("UPDATE flow_tests SET status = 'failed', note = ? WHERE id = ?",
                                ("Missed: the hub wasn't running at the start time", t["id"]))
                continue
            try:
                await self.rainbird.set_state("rainbird:controller", _run(t["zone"], t["minutes"]))
                self.db.execute("UPDATE flow_tests SET status = 'ran' WHERE id = ?", (t["id"],))
                started.append(t["id"])
            except HubError as exc:
                self.db.execute("UPDATE flow_tests SET status = 'failed', note = ? WHERE id = ?", (exc.message, t["id"]))
        self.db.commit()
        return started

    def update_measurements(self, hourly: dict[str, float], busy: set[str], now: datetime) -> int:
        done = 0
        for t in [x for x in self.tests() if x["status"] == "ran"]:
            result = measure(t, hourly, busy - {t["start"][:13]})
            if result:
                self.db.execute("UPDATE flow_tests SET status = 'measured', gallons = ?, gpm = ?, note = ? WHERE id = ?",
                                (result["gallons"], result["gpm"], f"Overnight use that hour is usually {result['baseline']} gal", t["id"]))
                done += 1
            elif now - datetime.fromisoformat(t["start"]) > GIVE_UP_AFTER:
                self.db.execute("UPDATE flow_tests SET status = 'failed', note = ? WHERE id = ?",
                                ("My Water Advisor hasn't posted hourly data for that hour", t["id"]))
        self.db.commit()
        return done

    def measured_gpm(self) -> dict[int, dict]:
        """Median of measured tests per zone: {"gpm", "tests", "last"}."""
        by_zone: dict[int, list[dict]] = {}
        for t in self.tests():
            if t["status"] == "measured" and t["gpm"]:
                by_zone.setdefault(t["zone"], []).append(t)
        return {z: {"gpm": round(statistics.median(x["gpm"] for x in ts), 2), "tests": len(ts), "last": ts[0]["start"]}
                for z, ts in by_zone.items()}

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()


def _run(zone: int, minutes: int):
    from .models import StateIn
    return StateIn(zone=zone, minutes=minutes)
