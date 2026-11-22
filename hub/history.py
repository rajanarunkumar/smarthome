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
        columns = {r[1] for r in db.execute("PRAGMA table_info(weather)")}
        if "tmean_f" not in columns:  # added for the energy analytics
            db.execute("ALTER TABLE weather ADD COLUMN tmean_f REAL")
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
            "INSERT INTO weather (day, rain_in, et0_in, tmax_f, tmean_f, forecast) "
            "VALUES (:day, :rain_in, :et0_in, :tmax_f, :tmean_f, :forecast) "
            "ON CONFLICT(day) DO UPDATE SET rain_in = excluded.rain_in, et0_in = excluded.et0_in, "
            "tmax_f = excluded.tmax_f, tmean_f = excluded.tmean_f, forecast = excluded.forecast",
            [{"tmean_f": None, **r} for r in rows])
        self.db.commit()

    def replace_inferred(self, runs: list[dict], first_day: date, last_day: date) -> None:
        """Swap in runs reconstructed from the controller's programs for days before the hub was watching."""
        self.db.execute("DELETE FROM watering WHERE source = 'inferred' AND started >= ? AND started < ?",
                        (first_day.isoformat(), (last_day + timedelta(days=1)).isoformat()))
        self.db.executemany("INSERT INTO watering (started, zone, minutes, source) VALUES (?, ?, ?, 'inferred')",
                            [(r["started"], r["zone"], r["minutes"]) for r in runs])
        self.db.commit()

    def first_observed_run(self) -> str | None:
        row = self.db.execute("SELECT MIN(started) AS m FROM watering WHERE source != 'inferred'").fetchone()
        return row["m"] if row else None

    # Reading
    def runs(self, since: date) -> list[dict]:
        rows = self.db.execute("SELECT started, zone, minutes, source FROM watering WHERE started >= ? ORDER BY started",
                               (since.isoformat(),))
        return [dict(r) for r in rows]

    def aligned_hourly(self, since: date, runs: list[dict] | None = None) -> tuple[dict[str, float], dict]:
        """Hourly water with the meter's clock corrected to match the sprinkler runs, plus how sure we are."""
        hourly = self.water_hourly(since)
        align = best_hour_shift(hourly, self.runs(since) if runs is None else runs)
        if align["confident"] and align["shift"]:
            hourly = apply_shift(hourly, align["shift"])
        return hourly, align

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


def _hour_index(key: str) -> int:
    return date.fromisoformat(key[:10]).toordinal() * 24 + int(key[11:13])


def _hour_key(index: int) -> str:
    ordinal, hour = divmod(index, 24)
    return f"{date.fromordinal(ordinal).isoformat()}T{hour:02d}"


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy) ** 0.5


def best_hour_shift(hourly: dict[str, float], runs: list[dict], max_shift: int = 12) -> dict:
    """Find how many hours the meter's clock is off from the sprinkler runs.

    The meter may stamp an hour by its end instead of its start, or in UTC with no label. For each
    shift s from -12 to +12, correlate the meter's gallons at stamp h with sprinkler minutes in true
    hour h + s; the shift with the strongest correlation wins if it's clearly better than none.
    true_hour = stamp_hour + shift.
    """
    watering = {_hour_index(h): sum(z.values()) for h, z in minutes_by_hour(runs).items()}
    overlap = sum(1 for h in minutes_by_hour(runs) if h in hourly)
    base = {"shift": 0, "r": None, "r_at_zero": None, "meter_hours": len(hourly),
            "watering_hours": len(watering), "overlap_hours": overlap, "confident": False}
    if len(watering) < 6 or len(hourly) < 48:
        return base
    stamps = [(_hour_index(h), v) for h, v in hourly.items()]
    scores = {}
    for s in range(-max_shift, max_shift + 1):
        r = pearson([watering.get(i + s, 0.0) for i, _ in stamps], [v for _, v in stamps])
        if r is not None:
            scores[s] = r
    if not scores:
        return base
    best = max(scores, key=lambda s: scores[s])
    r0 = scores.get(0)
    out = {**base, "shift": best, "r": round(scores[best], 2), "r_at_zero": round(r0, 2) if r0 is not None else None}
    out["confident"] = scores[best] >= 0.35 and (best == 0 or r0 is None or scores[best] - r0 >= 0.15)
    return out


def apply_shift(hourly: dict[str, float], shift: int) -> dict[str, float]:
    return {_hour_key(_hour_index(h) + shift): v for h, v in hourly.items()}


def baseline_by_hour_of_day(hourly: dict[str, float], busy: set[str], strict: bool = False) -> dict[int, float | None]:
    """Typical household use for each hour of the day, from hours without watering.

    strict: an hour of the day with fewer than 3 watering-free samples gets None instead of a guess. A
    program that waters in the same hour every day leaves no clean samples for that hour, and guessing
    from other hours' use would bias every zone that runs then.
    """
    by_hod: dict[int, list[float]] = defaultdict(list)
    for hour, gallons in hourly.items():
        if hour not in busy:
            by_hod[int(hour[11:13])].append(gallons)
    if strict:
        return {h: (statistics.median(by_hod[h]) if len(by_hod.get(h, [])) >= 3 else None) for h in range(24)}
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
    baseline = baseline_by_hour_of_day(hourly, set(by_hour), strict=True)
    rows = [(zones, hourly[h] - baseline[int(h[11:13])]) for h, zones in by_hour.items()
            if h in hourly and baseline[int(h[11:13])] is not None]
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
    # ± range: residual spread × the diagonal of (AᵀA)⁻¹, as in ordinary least squares.
    used = [(hz, ex) for hz, ex in rows if all(z in index for z in hz)]
    resid = [ex - sum(m * solution[index[z]] for z, m in hz.items()) for hz, ex in used]
    dof = len(used) - len(zones)
    sigma2 = sum(r * r for r in resid) / dof if dof > 0 else None
    out = {}
    for z, g in zip(zones, solution):
        if g <= 0.2:
            continue
        unit = [1.0 if i == index[z] else 0.0 for i in range(len(zones))]
        col = _solve(ata, unit)
        se = (sigma2 * col[index[z]]) ** 0.5 if sigma2 is not None and col and col[index[z]] > 0 else None
        out[z] = {"gpm": round(g, 1), "samples": counts[z], "plus_minus": round(1.96 * se, 1) if se is not None else None}
    return out


def confirm_with_meter(runs: list[dict], hourly: dict[str, float], min_extra_gallons: float = 10.0) -> list[dict]:
    """Keep reconstructed runs only where the water meter shows extra water in that hour.

    Rain delays, the rain sensor and manual stops don't leave a record on the
    controller, so a program that "should" have run may not have. Hours with no
    meter data aren't kept either.
    """
    by_hour = minutes_by_hour(runs)
    baseline = baseline_by_hour_of_day(hourly, set(by_hour))
    good_hours = {h for h in by_hour if h in hourly and hourly[h] - baseline[int(h[11:13])] >= min_extra_gallons}
    return [r for r in runs if datetime.fromisoformat(r["started"]).strftime("%Y-%m-%dT%H") in good_hours]


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


def data_checks(runs_all: list[dict], hourly: dict[str, float], align: dict, water_days: int,
                estimated: dict, measured: dict, weather_days: int, water_diag: dict | None) -> list[dict]:
    """Why the water-to-sprinkler correlation is, or isn't, being captured: one line per link in the chain.
    ok is True (fine), False (missing, with what to do), or None (can't tell yet)."""
    diag = water_diag or {}
    observed = sum(1 for r in runs_all if r.get("source") != "inferred")
    replayed = len(runs_all) - observed
    checks = []
    if runs_all:
        first = min(r["started"] for r in runs_all)[:10]
        checks.append({"id": "runs", "ok": True, "title": "Sprinkler runs recorded",
                       "detail": f"{observed} seen by the hub and {replayed} replayed from the controller's programs, since {first}."})
    else:
        checks.append({"id": "runs", "ok": False, "title": "No sprinkler runs recorded",
                       "detail": "The hub logs a run when it sees it start (it checks the controller every 30 seconds), and replays "
                                 "the controller's programs for past weeks once water data exists. Connect Rain Bird in Settings and keep the hub running."})
    if len(hourly) >= 24:
        checks.append({"id": "hourly", "ok": True, "title": "Hourly water readings",
                       "detail": f"{len(hourly)} hours from {min(hourly)[:10]} to {max(hourly)[:10]}."})
    else:
        if diag.get("hourly_failures") and not diag.get("hourly_days_with_data"):
            why = f"My Water Advisor refused or failed {diag['hourly_failures']} of {diag['hourly_days_requested']} hourly requests ({diag.get('last_error')})."
        elif diag.get("hourly_days_requested") and not diag.get("hourly_days_with_data"):
            why = "My Water Advisor answered but sent no hourly readings. The meter may report only daily totals, or post hours a day late."
        elif water_days:
            why = "Only daily totals so far. Tap Fetch last 4 weeks in the Insights tab."
        else:
            why = "Connect McKinney water in Settings, then tap Fetch last 4 weeks."
        checks.append({"id": "hourly", "ok": False, "title": "No hourly water readings",
                       "detail": why + " Gallons per minute for each zone needs hourly readings; until then use the flow tests below, "
                                       "which read the meter the next day."})
    overlap = align["overlap_hours"]
    if runs_all and hourly:
        checks.append({"id": "overlap", "ok": overlap >= 6, "title": "Watering hours with a meter reading",
                       "detail": f"{overlap} hour(s) have both sprinkler minutes and a water reading." if overlap >= 6 else
                                 f"Only {overlap} watering hour(s) have a meter reading. The meter posts data late, or the clocks don't match (see next line)."})
    else:
        checks.append({"id": "overlap", "ok": None, "title": "Watering hours with a meter reading", "detail": "Needs sprinkler runs and hourly water first."})
    if align["confident"]:
        text = (f"The meter's hours line up with the sprinkler runs (correlation {align['r']})." if align["shift"] == 0 else
                f"The meter's clock was off by {align['shift']:+d} hour(s) compared with the sprinklers. The hub corrected for it "
                f"(correlation {align['r']} vs {align['r_at_zero']} before).")
        checks.append({"id": "clock", "ok": True, "title": "Meter clock matches the sprinklers", "detail": text})
    elif align["watering_hours"] >= 6 and align["meter_hours"] >= 48:
        checks.append({"id": "clock", "ok": False, "title": "Couldn't match the meter's hours to the sprinklers",
                       "detail": f"Water use doesn't rise clearly when the sprinklers run (best correlation {align['r']}). Either the zones use very little "
                                 "water compared with the house, or the meter data is too coarse. Flow tests will tell."})
    else:
        checks.append({"id": "clock", "ok": None, "title": "Meter clock check",
                       "detail": "Needs about 6 watering hours and 2 days of hourly water readings."})
    zones = {r["zone"] for r in runs_all}
    unknown = sorted(zones - set(estimated) - set(measured))
    if not zones:
        checks.append({"id": "zones", "ok": None, "title": "Gallons per minute by zone", "detail": "Needs sprinkler runs first."})
    elif not unknown:
        checks.append({"id": "zones", "ok": True, "title": "Gallons per minute by zone", "detail": f"Known for all {len(zones)} zone(s)."})
    elif overlap >= 6:
        checks.append({"id": "zones", "ok": False, "title": "Gallons per minute by zone",
                       "detail": f"Couldn't separate zone(s) {', '.join(map(str, unknown))}: they always run back to back in the same proportions, "
                                 "or have fewer than 3 watering hours, or run in an hour that is watered every day, so there is no "
                                 "normal-use day to compare with. Run flow tests to measure each zone alone."})
    else:
        checks.append({"id": "zones", "ok": None, "title": "Gallons per minute by zone", "detail": "Waiting for the steps above."})
    checks.append({"id": "weather", "ok": weather_days > 0, "title": "Weather history",
                   "detail": f"{weather_days} days." if weather_days else "Set your city in Insights to get rain and evaporation."})
    return checks


def build_insights(store: HistoryStore, zone_label, areas: dict[int, float], days: int = 30,
                   today: date | None = None, measured: dict[int, dict] | None = None,
                   photo_advice=None, water_diag: dict | None = None) -> dict:
    """Everything the Insights tab shows, for the last `days` days plus the forecast."""
    today = today or date.today()
    since = today - timedelta(days=days - 1)
    learn_since = today - timedelta(days=90)  # flow estimates use more history than the chart shows
    runs_all = store.runs(learn_since)
    hourly_all, align = store.aligned_hourly(learn_since, runs_all)
    estimated = zone_flow(runs_all, hourly_all)
    measured = measured or {}
    # A flow test beats the statistical estimate: it's a direct measurement.
    flow = {**estimated, **{z: {"gpm": m["gpm"], "samples": m["tests"]} for z, m in measured.items()}}
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
    measured_days = [r for r in past if r["total_gal"] is not None]
    total = sum(r["total_gal"] for r in measured_days)
    sprinkler = sum(r["sprinkler_gal"] for r in measured_days)
    last7 = [r for r in series if not r["forecast"]][-7:]
    need_week = max(sum(r["et0_in"] or 0 for r in last7) * TURF_KC - sum(r["rain_in"] or 0 for r in last7) * EFFECTIVE_RAIN, 0) if last7 else None
    temps = [w.get("tmean_f") for d, w in weather.items() if d <= today.isoformat() and w.get("tmean_f") is not None][-7:]
    tmean7 = sum(temps) / len(temps) if temps else None
    zones = []
    for z in sorted(set(zone_totals) | set(flow) | set(areas)):
        gpm = flow.get(z, {}).get("gpm")
        minutes = zone_totals[z]["minutes"]
        gallons = minutes * gpm if gpm else None
        inches = gallons / (areas[z] * GALLONS_PER_SQFT_INCH) if gallons and areas.get(z) else None
        week_in = (week_minutes.get(z, 0) * gpm / (areas[z] * GALLONS_PER_SQFT_INCH)) if gpm and areas.get(z) else None
        zones.append({"zone": z, "label": zone_label(z), "runs": zone_totals[z]["runs"], "minutes": round(minutes),
                      "gpm": gpm, "samples": flow.get(z, {}).get("samples", 0),
                      "gpm_source": "flow test" if z in measured else ("estimated" if z in estimated else None),
                      "estimated_gpm": estimated.get(z, {}).get("gpm"), "plus_minus": estimated.get(z, {}).get("plus_minus"),
                      "measured_gpm": measured.get(z, {}).get("gpm"), "flow_tests": measured.get(z, {}).get("tests", 0),
                      "gallons": round(gallons) if gallons else None, "inches": round(inches, 2) if inches else None,
                      "week_in": round(week_in, 2) if week_in is not None else None,
                      "area_sqft": areas.get(z),
                      "advice": photo_advice(z, week_in, need_week, tmean7) if photo_advice else None})
    return {
        "days": days,
        "summary": {
            "total_gal": round(total) if measured_days else None,
            "sprinkler_gal": round(sprinkler) if measured_days else None,
            "sprinkler_share": round(sprinkler / total, 2) if measured_days and total else None,
            "rain_in": round(sum(r["rain_in"] or 0 for r in past), 2),
            "et0_in": round(sum(r["et0_in"] or 0 for r in past), 2),
            "minutes": round(sum(r["minutes"] for r in past)),
        },
        "series": series, "zones": zones, "leak": leak,
        "recommendations": recommendations(series, flow, dict(week_minutes), areas, zone_label, leak, today.isoformat()),
        "need_week_in": round(need_week, 2) if need_week is not None else None, "tmean7_f": round(tmean7) if tmean7 else None,
        "has_water": bool(measured_days), "has_hourly": bool(hourly_all), "has_weather": bool(weather),
        "has_runs": bool(runs_all), "alignment": align,
        "checks": data_checks(runs_all, hourly_all, align, len(measured_days), estimated, measured, len(weather), water_diag),
    }
