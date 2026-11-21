"""History the hub keeps for analytics: sprinkler runs, water use, and weather.

Everything is stored in the hub's SQLite database (~/.smarthome/hub.db).
The analysis functions are pure so they can be tested without devices.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

GALLONS_PER_SQFT_INCH = 0.623   # 1 inch of water over 1 sq ft
TURF_KC = 0.7                   # crop coefficient for warm-season turf (Bermuda, St. Augustine)
EFFECTIVE_RAIN = 0.8            # share of rain the soil keeps
RAIN_SKIP_INCHES = 0.5          # rain in the last 48 h that makes watering unnecessary
FORECAST_SKIP_INCHES = 0.25     # rain expected in the next 24 h
LEAK_GALLONS_PER_HOUR = 2.0     # steady overnight use above this suggests a leak


class HistoryStore:
    def __init__(self, db) -> None:
        self.db = db
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS watering (
                id INTEGER PRIMARY KEY, started TEXT NOT NULL, zone INTEGER NOT NULL,
                minutes REAL NOT NULL, source TEXT NOT NULL DEFAULT 'controller');
            CREATE INDEX IF NOT EXISTS watering_started ON watering(started);
            CREATE TABLE IF NOT EXISTS water_hourly (
                hour TEXT PRIMARY KEY, gallons REAL NOT NULL);   -- 'YYYY-MM-DDTHH'
            CREATE TABLE IF NOT EXISTS water_daily (
                day TEXT PRIMARY KEY, gallons REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS weather (
                day TEXT PRIMARY KEY, rain_in REAL, et0_in REAL, tmax_f REAL, forecast INTEGER NOT NULL DEFAULT 0);
            """
        )
        db.commit()

    # Writing
    def log_run(self, started: datetime, zone: int, minutes: float, source: str = "controller") -> None:
        if minutes < 0.5:
            return  # a blip, not a run
        self.db.execute("INSERT INTO watering (started, zone, minutes, source) VALUES (?, ?, ?, ?)",
                        (started.isoformat(timespec="minutes"), zone, round(minutes, 1), source))
        self.db.commit()

    def put_water_hourly(self, rows: Iterable[tuple[str, float]]) -> None:
        self.db.executemany("INSERT INTO water_hourly (hour, gallons) VALUES (?, ?) "
                            "ON CONFLICT(hour) DO UPDATE SET gallons = excluded.gallons", list(rows))
        self.db.commit()

    def put_water_daily(self, rows: Iterable[tuple[str, float]]) -> None:
        self.db.executemany("INSERT INTO water_daily (day, gallons) VALUES (?, ?) "
                            "ON CONFLICT(day) DO UPDATE SET gallons = excluded.gallons", list(rows))
        self.db.commit()

    def put_weather(self, rows: Iterable[dict]) -> None:
        self.db.executemany(
            "INSERT INTO weather (day, rain_in, et0_in, tmax_f, forecast) VALUES (:day, :rain_in, :et0_in, :tmax_f, :forecast) "
            "ON CONFLICT(day) DO UPDATE SET rain_in = excluded.rain_in, et0_in = excluded.et0_in, "
            "tmax_f = excluded.tmax_f, forecast = excluded.forecast", list(rows))
        self.db.commit()

    # Reading
    def runs(self, since: date) -> list[dict]:
        rows = self.db.execute("SELECT started, zone, minutes, source FROM watering WHERE started >= ? ORDER BY started",
                               (since.isoformat(),))
        return [dict(r) for r in rows]

    def water_hourly(self, since: date) -> dict[str, float]:
        rows = self.db.execute("SELECT hour, gallons FROM water_hourly WHERE hour >= ?", (since.isoformat(),))
        return {r["hour"]: r["gallons"] for r in rows}

    def water_daily(self, since: date) -> dict[str, float]:
        rows = self.db.execute("SELECT day, gallons FROM water_daily WHERE day >= ?", (since.isoformat(),))
        return {r["day"]: r["gallons"] for r in rows}

    def weather(self, since: date) -> dict[str, dict]:
        rows = self.db.execute("SELECT * FROM weather WHERE day >= ? ORDER BY day", (since.isoformat(),))
        return {r["day"]: dict(r) for r in rows}


# Analysis
def minutes_by_hour(runs: list[dict]) -> dict[str, dict[int, float]]:
    """Split each run across the clock hours it covers: {'YYYY-MM-DDTHH': {zone: minutes}}."""
    out: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for run in runs:
        start = datetime.fromisoformat(run["started"])
        end = start + timedelta(minutes=run["minutes"])
        t = start
        while t < end:
            hour_end = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            chunk_end = min(end, hour_end)
            out[t.strftime("%Y-%m-%dT%H")][run["zone"]] += (chunk_end - t).total_seconds() / 60
            t = chunk_end
    return out


def baseline_by_hour_of_day(hourly: dict[str, float], busy: set[str]) -> dict[int, float]:
    """Typical household use for each hour of the day, from hours without watering."""
    by_hod: dict[int, list[float]] = defaultdict(list)
    for hour, gallons in hourly.items():
        if hour not in busy:
            by_hod[int(hour[11:13])].append(gallons)
    overall = statistics.median([g for v in by_hod.values() for g in v]) if by_hod else 0.0
    return {h: (statistics.median(by_hod[h]) if by_hod.get(h) else overall) for h in range(24)}


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; None when the system is singular."""
    n = len(vector)
    m = [row[:] + [v] for row, v in zip(matrix, vector)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-9:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [a - f * b for a, b in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]


def zone_flow(runs: list[dict], hourly: dict[str, float], min_samples: int = 3) -> dict[int, dict]:
    """Estimate gallons per minute for each zone.

    Each hour with watering gives one equation: water used above that hour's
    normal household use = sum over zones of (minutes run × that zone's flow).
    Rain Bird runs zones back to back, so most hours hold several zones; a
    least-squares fit over all those hours separates them. A zone needs at
    least `min_samples` hours, and the hours must not always pair the same
    zones together, or it isn't estimated. `samples` is the hour count.
    """
    by_hour = minutes_by_hour(runs)
    baseline = baseline_by_hour_of_day(hourly, set(by_hour))
    rows = [(zones, hourly[h] - baseline[int(h[11:13])]) for h, zones in by_hour.items() if h in hourly]
    counts: dict[int, int] = defaultdict(int)
    for zones, _ in rows:
        for z, minutes in zones.items():
            if minutes >= 1:
                counts[z] += 1
    zones = sorted(z for z, c in counts.items() if c >= min_samples)
    if not zones:
        return {}
    index = {z: i for i, z in enumerate(zones)}
    ata = [[0.0] * len(zones) for _ in zones]
    atb = [0.0] * len(zones)
    for hour_zones, excess in rows:
        if any(z not in index for z in hour_zones):
            continue  # an hour with a zone we can't solve for would skew the others
        for zi, mi in hour_zones.items():
            atb[index[zi]] += mi * excess
            for zj, mj in hour_zones.items():
                ata[index[zi]][index[zj]] += mi * mj
    solution = _solve(ata, atb)
    if solution is None:
        return {}
    return {z: {"gpm": round(g, 1), "samples": counts[z]} for z, g in zip(zones, solution) if g > 0.2}


def daily_series(days: list[str], runs: list[dict], water_daily: dict[str, float], weather: dict[str, dict],
                 flow: dict[int, dict]) -> list[dict]:
    """One row per day: total water, estimated sprinkler water, the rest, minutes, rain, ET."""
    minutes: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for run in runs:
        minutes[run["started"][:10]][run["zone"]] += run["minutes"]
    out = []
    for day in days:
        zone_minutes = minutes.get(day, {})
        sprinkler = sum(m * flow[z]["gpm"] for z, m in zone_minutes.items() if z in flow)
        total = water_daily.get(day)
        w = weather.get(day, {})
        out.append({
            "day": day,
            "total_gal": total,
            "sprinkler_gal": round(min(sprinkler, total), 0) if total is not None else round(sprinkler, 0),
            "other_gal": round(max(total - sprinkler, 0), 0) if total is not None else None,
            "minutes": round(sum(zone_minutes.values()), 0),
            "rain_in": w.get("rain_in"), "et0_in": w.get("et0_in"), "tmax_f": w.get("tmax_f"),
            "forecast": bool(w.get("forecast")),
        })
    return out


def overnight_leak(hourly: dict[str, float], busy: set[str], nights: int = 7) -> dict | None:
    """Lowest use between 1 and 5 AM on recent nights; steady flow there means something is running or leaking."""
    lows = []
    for day in sorted({h[:10] for h in hourly})[-nights:]:
        values = [hourly[f"{day}T{h:02d}"] for h in range(1, 5)
                  if f"{day}T{h:02d}" in hourly and f"{day}T{h:02d}" not in busy]
        if len(values) >= 3:
            lows.append(min(values))
    if len(lows) < 3:
        return None
    floor = statistics.median(lows)
    return {"gallons_per_hour": round(floor, 1), "nights": len(lows), "suspect": floor >= LEAK_GALLONS_PER_HOUR}


def recommendations(series: list[dict], flow: dict[int, dict], zone_minutes_week: dict[int, float],
                    areas: dict[int, float], zone_label, leak: dict | None, today: str) -> list[dict]:
    """Plain-language suggestions with a severity: 'good', 'warning', 'serious'."""
    recs: list[dict] = []
    past = [r for r in series if r["day"] <= today and not r["forecast"]]
    last7 = past[-7:]
    rain48 = sum(r["rain_in"] or 0 for r in past[-2:])
    tomorrow = next((r for r in series if r["day"] > today), None)
    if rain48 >= RAIN_SKIP_INCHES:
        recs.append({"level": "good", "title": "Skip watering for now",
                     "text": f"{rain48:.2f} in of rain fell in the last two days. Smart programs skip on their own; "
                             "set a 1–2 day rain delay if the controller's own programs are on."})
    elif tomorrow and (tomorrow["rain_in"] or 0) >= FORECAST_SKIP_INCHES:
        recs.append({"level": "good", "title": "Rain is on the way",
                     "text": f"{tomorrow['rain_in']:.2f} in is forecast for {tomorrow['day']}. Holding off a day saves water."})
    et_week = sum(r["et0_in"] or 0 for r in last7)
    rain_week = sum(r["rain_in"] or 0 for r in last7)
    need_week = max(et_week * TURF_KC - rain_week * EFFECTIVE_RAIN, 0)
    if last7:
        recs.append({"level": "good", "title": f"Lawn needs about {need_week:.2f} in this week",
                     "text": f"Evaporation {et_week:.2f} in × turf factor {TURF_KC} − effective rain "
                             f"{rain_week * EFFECTIVE_RAIN:.2f} in, over the last 7 days."})
    for zone, minutes in sorted(zone_minutes_week.items()):
        if zone not in flow or not areas.get(zone):
            continue
        applied = minutes * flow[zone]["gpm"] / (areas[zone] * GALLONS_PER_SQFT_INCH)
        if need_week <= 0 and applied > 0:
            recs.append({"level": "warning", "title": f"{zone_label(zone)}: rain covered it",
                         "text": f"It got {applied:.2f} in from the sprinklers this week when rain alone was enough."})
        elif need_week > 0 and abs(applied - need_week) / need_week > 0.25:
            suggested = round(minutes * need_week / applied) if applied else None
            more = applied < need_week
            recs.append({"level": "warning" if not more else "serious",
                         "title": f"{zone_label(zone)}: {'under' if more else 'over'}watered",
                         "text": f"About {applied:.2f} in applied vs {need_week:.2f} in needed. "
                                 + (f"Try {suggested} min a week instead of {minutes:.0f}." if suggested else "")})
    missing = [z for z in zone_minutes_week if z not in flow]
    if missing:
        recs.append({"level": "warning", "title": "Still learning some zones",
                     "text": "Flow isn't known yet for " + ", ".join(zone_label(z) for z in missing)
                             + ". It needs a few runs while the water meter reports hourly use."})
    if any(z in flow and not areas.get(z) for z in zone_minutes_week):
        recs.append({"level": "warning", "title": "Add zone sizes for inch-based advice",
                     "text": "Enter each zone's area in square feet in the Sprinklers screen, so water can be compared "
                             "to what the lawn actually needs."})
    if leak and leak["suspect"]:
        recs.append({"level": "serious", "title": "Possible leak",
                     "text": f"Water keeps flowing overnight: about {leak['gallons_per_hour']} gal/hour between 1 and 5 AM "
                             f"on {leak['nights']} recent nights, with no watering. Check toilets, the softener, and the irrigation valves."})
    return recs


def build_insights(store: HistoryStore, zone_label, areas: dict[int, float], days: int = 30,
                   today: date | None = None) -> dict:
    """Everything the Insights tab shows, for the last `days` days plus the forecast."""
    today = today or date.today()
    since = today - timedelta(days=days - 1)
    learn_since = today - timedelta(days=90)  # flow estimates use more history than the chart shows
    runs_all = store.runs(learn_since)
    hourly_all = store.water_hourly(learn_since)
    flow = zone_flow(runs_all, hourly_all)
    runs = [r for r in runs_all if r["started"][:10] >= since.isoformat()]
    weather = store.weather(since)
    day_list = sorted({(since + timedelta(days=n)).isoformat() for n in range(days)}
                      | {d for d in weather if d > today.isoformat()})
    series = daily_series(day_list, runs, store.water_daily(since), weather, flow)
    week_start = (today - timedelta(days=6)).isoformat()
    week_minutes: dict[int, float] = defaultdict(float)
    zone_totals: dict[int, dict] = defaultdict(lambda: {"minutes": 0.0, "runs": 0})
    for r in runs:
        zone_totals[r["zone"]]["minutes"] += r["minutes"]
        zone_totals[r["zone"]]["runs"] += 1
        if r["started"][:10] >= week_start:
            week_minutes[r["zone"]] += r["minutes"]
    leak = overnight_leak(hourly_all, set(minutes_by_hour(runs_all)))
    past = [r for r in series if not r["forecast"]]
    measured = [r for r in past if r["total_gal"] is not None]
    total = sum(r["total_gal"] for r in measured)
    sprinkler = sum(r["sprinkler_gal"] for r in measured)
    zones = []
    for z in sorted(set(zone_totals) | set(flow) | set(areas)):
        gpm = flow.get(z, {}).get("gpm")
        minutes = zone_totals[z]["minutes"]
        gallons = minutes * gpm if gpm else None
        inches = gallons / (areas[z] * GALLONS_PER_SQFT_INCH) if gallons and areas.get(z) else None
        zones.append({"zone": z, "label": zone_label(z), "runs": zone_totals[z]["runs"], "minutes": round(minutes),
                      "gpm": gpm, "samples": flow.get(z, {}).get("samples", 0),
                      "gallons": round(gallons) if gallons else None, "inches": round(inches, 2) if inches else None,
                      "area_sqft": areas.get(z)})
    return {
        "days": days,
        "summary": {
            "total_gal": round(total) if measured else None,
            "sprinkler_gal": round(sprinkler) if measured else None,
            "sprinkler_share": round(sprinkler / total, 2) if measured and total else None,
            "rain_in": round(sum(r["rain_in"] or 0 for r in past), 2),
            "et0_in": round(sum(r["et0_in"] or 0 for r in past), 2),
            "minutes": round(sum(r["minutes"] for r in past)),
        },
        "series": series, "zones": zones, "leak": leak,
        "recommendations": recommendations(series, flow, dict(week_minutes), areas, zone_label, leak, today.isoformat()),
        "has_water": bool(measured), "has_hourly": bool(hourly_all), "has_weather": bool(weather),
        "has_runs": bool(runs_all),
    }
