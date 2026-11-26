"""Smart watering programs run by the hub, for any Rain Bird controller.

A program has start times, days, and minutes per zone, set for peak summer.
At each start the hub decides, using the weather history:
  - skip, when 0.5 in or more of rain fell in the last two days, or 0.25 in
    or more is forecast for today;
  - otherwise scale every zone's minutes by the last week's evaporation
    compared to peak summer (0.25 in/day), between 30% and 120%.
Every decision is logged with its reason. Like other hub schedules, these
run only while the hub is running. Turn the controller's own programs off in
the Rain Bird app if you use these, or the lawn gets watered twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta

from .history import HistoryStore
from .models import DAYS, HubError

log = logging.getLogger(__name__)
CHECK_SECONDS = 20
PEAK_ET_PER_DAY = 0.25
SKIP_RAIN_48H = 0.5
SKIP_FORECAST_TODAY = 0.25
MIN_FACTOR, MAX_FACTOR = 0.3, 1.2


def decide(weather: dict[str, dict], today: date, peak_et: float = PEAK_ET_PER_DAY) -> dict:
    """{"run": bool, "factor": float, "reason": str} from daily weather rows keyed by YYYY-MM-DD."""
    day = lambda n: weather.get((today - timedelta(days=n)).isoformat(), {})
    rain48 = sum((day(n).get("rain_in") or 0) for n in (1, 2))
    today_rain = day(0).get("rain_in") or 0
    if rain48 >= SKIP_RAIN_48H:
        return {"run": False, "factor": 0.0, "reason": f"Skipped: {rain48:.2f} in of rain in the last two days"}
    if today_rain >= SKIP_FORECAST_TODAY:
        return {"run": False, "factor": 0.0, "reason": f"Skipped: {today_rain:.2f} in of rain expected today"}
    week = [day(n).get("et0_in") for n in range(1, 8)]
    week = [x for x in week if x is not None]
    if not week:
        return {"run": True, "factor": 1.0, "reason": "Ran at full time (no weather history yet)"}
    avg = sum(week) / len(week)
    factor = round(max(MIN_FACTOR, min(MAX_FACTOR, avg / peak_et)), 2)
    return {"run": True, "factor": factor,
            "reason": f"Ran at {factor:.0%}: evaporation averaged {avg:.2f} in/day vs {peak_et:.2f} at peak summer"}


class SmartPrograms:
    def __init__(self, db, history: HistoryStore, rainbird) -> None:
        self.db, self.history, self.rainbird = db, history, rainbird
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS irrigation_programs (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, starts TEXT NOT NULL, days TEXT NOT NULL,
                zones TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, smart INTEGER NOT NULL DEFAULT 1,
                last_run TEXT);
            CREATE TABLE IF NOT EXISTS irrigation_decisions (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, program_id INTEGER, ran INTEGER NOT NULL,
                factor REAL, reason TEXT NOT NULL);
            """
        )
        db.commit()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due(datetime.now())
            except Exception as exc:  # noqa: BLE001
                log.warning("Smart programs: %s", exc)
            await asyncio.sleep(CHECK_SECONDS)

    def _row(self, r) -> dict:
        d = dict(r)
        d.update(starts=json.loads(d["starts"]), days=json.loads(d["days"]), zones=json.loads(d["zones"]),
                 enabled=bool(d["enabled"]), smart=bool(d["smart"]))
        return d

    def programs(self) -> list[dict]:
        return [self._row(r) for r in self.db.execute("SELECT * FROM irrigation_programs ORDER BY id")]

    def decisions(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT * FROM irrigation_decisions ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def save(self, program: dict, program_id: int | None = None) -> dict:
        if not program["zones"] or not any(z["minutes"] > 0 for z in program["zones"]):
            raise HubError(400, "Add at least one zone with minutes.")
        if not program["starts"]:
            raise HubError(400, "Add at least one start time.")
        values = (program["name"], json.dumps(sorted(program["starts"])), json.dumps(program["days"]),
                  json.dumps(program["zones"]), int(program.get("enabled", True)), int(program.get("smart", True)))
        if program_id is None:
            cur = self.db.execute("INSERT INTO irrigation_programs (name, starts, days, zones, enabled, smart) "
                                  "VALUES (?, ?, ?, ?, ?, ?)", values)
            program_id = cur.lastrowid
        else:
            self.db.execute("UPDATE irrigation_programs SET name=?, starts=?, days=?, zones=?, enabled=?, smart=? "
                            "WHERE id=?", (*values, program_id))
        self.db.commit()
        return next(p for p in self.programs() if p["id"] == program_id)

    def delete(self, program_id: int) -> None:
        self.db.execute("DELETE FROM irrigation_programs WHERE id = ?", (program_id,))
        self.db.commit()

    def _log(self, program_id: int, ran: bool, factor: float, reason: str, now: datetime) -> None:
        self.db.execute("INSERT INTO irrigation_decisions (at, program_id, ran, factor, reason) VALUES (?, ?, ?, ?, ?)",
                        (now.isoformat(timespec="minutes"), program_id, int(ran), factor, reason))
        self.db.commit()

    def start_program(self, program: dict, now: datetime, smart: bool | None = None) -> dict:
        smart = program["smart"] if smart is None else smart
        decision = decide(self.history.weather(now.date() - timedelta(days=9)), now.date()) if smart else \
            {"run": True, "factor": 1.0, "reason": "Ran at full time (weather adjustment is off)"}
        if decision["run"]:
            zones = [(int(z["zone"]), max(1, round(z["minutes"] * decision["factor"])))
                     for z in program["zones"] if z["minutes"] > 0]
            self.rainbird.run_sequence(zones, source="hub")
        self._log(program["id"], decision["run"], decision["factor"], decision["reason"], now)
        return decision

    async def run_due(self, now: datetime) -> list[int]:
        stamp, hhmm, day = now.strftime("%Y-%m-%d %H:%M"), now.strftime("%H:%M"), DAYS[now.weekday()]
        started = []
        for program in self.programs():
            if not program["enabled"] or hhmm not in program["starts"] or day not in program["days"] \
                    or program["last_run"] == stamp:
                continue
            self.db.execute("UPDATE irrigation_programs SET last_run = ? WHERE id = ?", (stamp, program["id"]))
            self.db.commit()
            self.start_program(program, now)
            started.append(program["id"])
        return started

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
