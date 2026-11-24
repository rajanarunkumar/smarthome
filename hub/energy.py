"""Electricity analytics: what used the power.

Inputs, all kept in ~/.smarthome/hub.db:
- 15-minute kWh from Smart Meter Texas (TXU's meter), backfilled 4 weeks;
- device activity: when the washer, dryer, pool pump, AC, plugs, TV… were
  running, recorded by the hub every 2 minutes from the devices' own state;
- daily mean temperature (Open-Meteo), for heating and cooling.

From those:
- kW per device: each 15-minute slot with devices running gives one equation,
  power above the nearby idle level = Σ (share of the slot the device ran ×
  its kW). Least squares over all such slots separates devices.
- HVAC: daily kWh not explained by tracked devices = base + a × cooling
  degree-days + b × heating degree-days (65 °F). The Lennox thermostat isn't
  connected yet, so HVAC is estimated from the weather.
- Always-on load: the lowest power between 1 and 5 AM.
- Spikes: 15-minute slots well above normal, with what was running then.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from .history import _solve

BALANCE_F = 65.0
SLOT = timedelta(minutes=15)
TRACKED_KINDS = {"appliance", "pool", "purifier", "plug", "dimmer", "tv", "vacuum", "thermostat"}
# Big enough to see in the meter's 15-minute readings. LED lights (5–60 W), TVs, purifiers and robot vacuums are far
# smaller than the meter's normal swings, so their "measured" power would only be whatever else ran at the same time.
MEASURABLE_KINDS = {"appliance", "pool", "plug", "thermostat"}
MAX_KW = {"plug": 2.0, "appliance": 7.0, "pool": 4.0, "thermostat": 15.0}   # more than this is something else
MIN_KW = 0.15
LAUNDRY_IDLE = {"POWER_OFF", "INITIAL", "END", "PAUSE", "SLEEP", "ERROR", "STANDBY"}


class EnergyStore:
    def __init__(self, db) -> None:
        self.db = db
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS energy_interval (slot TEXT PRIMARY KEY, kwh REAL NOT NULL); -- 'YYYY-MM-DDTHH:MM'
            CREATE TABLE IF NOT EXISTS device_activity (
                id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                start TEXT NOT NULL, end TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS device_activity_start ON device_activity(start);
            CREATE TABLE IF NOT EXISTS device_watts (device_id TEXT PRIMARY KEY, watts REAL NOT NULL); -- entered by the owner
            CREATE TABLE IF NOT EXISTS device_estimated (  -- bulbs: average power from their model and brightness while on
                device_id TEXT PRIMARY KEY, watts REAL NOT NULL, samples INTEGER NOT NULL, updated TEXT NOT NULL,
                standby REAL NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'measured', profile TEXT);
            CREATE TABLE IF NOT EXISTS device_reported (  -- average power the device itself reported while on
                device_id TEXT PRIMARY KEY, watts REAL NOT NULL, samples INTEGER NOT NULL, updated TEXT NOT NULL);
            """
        )
        db.commit()
        self._open: dict[str, tuple[str, str, datetime]] = {}  # device id -> (name, kind, since)

    def put_intervals(self, rows) -> None:
        self.db.executemany("INSERT INTO energy_interval (slot, kwh) VALUES (?, ?) "
                            "ON CONFLICT(slot) DO UPDATE SET kwh = excluded.kwh", list(rows))
        self.db.commit()

    def interval_days(self, since: date) -> set[str]:
        rows = self.db.execute("SELECT DISTINCT substr(slot, 1, 10) AS d FROM energy_interval WHERE slot >= ?",
                               (since.isoformat(),))
        return {r["d"] for r in rows}

    def intervals(self, since: date) -> dict[str, float]:
        rows = self.db.execute("SELECT slot, kwh FROM energy_interval WHERE slot >= ?", (since.isoformat(),))
        return {r["slot"]: r["kwh"] for r in rows}

    def activity(self, since: date) -> list[dict]:
        rows = self.db.execute("SELECT device_id, name, kind, start, end FROM device_activity WHERE end >= ? ORDER BY start",
                               (since.isoformat(),))
        return [dict(r) for r in rows]

    def watts(self) -> dict[str, float]:
        """Power the owner entered for devices (e.g. an incandescent chandelier), in watts."""
        return {r["device_id"]: r["watts"] for r in self.db.execute("SELECT device_id, watts FROM device_watts")}

    def reported(self, min_samples: int = 3) -> dict[str, float]:
        """Average watts devices measured themselves while on (smart plugs with energy monitoring, the pool pump)."""
        return {r["device_id"]: r["watts"] for r in self.db.execute(
            "SELECT device_id, watts FROM device_reported WHERE samples >= ?", (min_samples,))}

    def estimated(self) -> dict[str, dict]:
        """Smart bulbs: average watts while on from their model's measured profile at the brightness used."""
        return {r["device_id"]: dict(r) for r in self.db.execute(
            "SELECT device_id, watts, standby, source, profile FROM device_estimated WHERE samples >= 2")}

    def known_watts(self) -> dict[str, float]:
        """What's known without guessing the meter: bulb profiles, then what devices report, then what the owner
        entered (each overrides the one before)."""
        return {**{d: e["watts"] for d, e in self.estimated().items()}, **self.reported(), **self.watts()}

    def _sample(self, device_id: str, watts: float, now: datetime, table: str = "device_reported", extra: dict | None = None) -> None:
        row = self.db.execute(f"SELECT watts, samples FROM {table} WHERE device_id = ?", (device_id,)).fetchone()
        extra = extra or {}
        if row is None:
            cols = ", ".join(["device_id", "watts", "samples", "updated", *extra])
            self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * (4 + len(extra)))})",
                            (device_id, watts, 1, now.isoformat(timespec="minutes"), *extra.values()))
            return
        n = min(row["samples"], 500)     # running average that keeps following slow changes (pump speed, dimming)
        sets = "".join(f", {k} = ?" for k in extra)
        self.db.execute(f"UPDATE {table} SET watts = ?, samples = ?, updated = ?{sets} WHERE device_id = ?",
                        ((row["watts"] * n + watts) / (n + 1), row["samples"] + 1, now.isoformat(timespec="minutes"),
                         *extra.values(), device_id))

    def set_watts(self, device_id: str, watts: float | None) -> None:
        if watts:
            self.db.execute("INSERT INTO device_watts (device_id, watts) VALUES (?, ?) "
                            "ON CONFLICT(device_id) DO UPDATE SET watts = excluded.watts", (device_id, watts))
        else:
            self.db.execute("DELETE FROM device_watts WHERE device_id = ?", (device_id,))
        self.db.commit()

    def observe(self, devices, now: datetime) -> None:
        """Record running periods from a device snapshot (called every couple of minutes). Lights are recorded
        only when their wattage was entered."""
        from .lights import profile, watts_at
        seen = set()
        entered = self.watts()
        for d in devices:
            bulb = d.kind == "light" and d.id not in entered
            if (d.kind not in TRACKED_KINDS and d.id not in entered and not bulb) or not d.online:
                continue
            if d.on is not True:
                continue
            if d.watts is not None and d.watts >= 2:      # below that it's the plug's own standby, not the load
                self._sample(d.id, float(d.watts), now)
            elif bulb:
                prof = profile(d.model_id, d.model, d.name)
                self._sample(d.id, watts_at(prof, d.brightness), now, "device_estimated",
                             {"standby": prof["standby"], "source": prof["source"], "profile": prof["name"]})
            seen.add(d.id)
            if d.id not in self._open:
                self._open[d.id] = (d.name, d.kind, now)
        for device_id in list(self._open):
            if device_id not in seen:
                name, kind, since = self._open.pop(device_id)
                if now - since >= timedelta(minutes=2):
                    self.db.execute("INSERT INTO device_activity (device_id, name, kind, start, end) VALUES (?, ?, ?, ?, ?)",
                                    (device_id, name, kind, since.isoformat(timespec="minutes"),
                                     now.isoformat(timespec="minutes")))
        self.db.commit()


# Analysis
def slot_key(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M")


def activity_by_slot(activity: list[dict]) -> dict[str, dict[str, float]]:
    """{slot: {device_id: share of the 15 minutes it ran}}."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for a in activity:
        start, end = datetime.fromisoformat(a["start"]), datetime.fromisoformat(a["end"])
        t = start.replace(minute=start.minute - start.minute % 15, second=0, microsecond=0)
        while t < end:
            overlap = (min(end, t + SLOT) - max(start, t)).total_seconds() / SLOT.total_seconds()
            if overlap > 0:
                key = slot_key(t)
                out[key][a["device_id"]] = min(1.0, out[key].get(a["device_id"], 0) + overlap)
            t += SLOT
    return out


def local_idle(intervals: dict[str, float], busy: set[str], slot: str, hours: int = 2) -> float | None:
    """Median kWh of idle slots within ±hours of `slot` (follows the HVAC's slow drift)."""
    t = datetime.fromisoformat(slot)
    values = []
    k = t - timedelta(hours=hours)
    while k <= t + timedelta(hours=hours):
        key = slot_key(k)
        if key in intervals and key not in busy:
            values.append(intervals[key])
        k += SLOT
    return statistics.median(values) if len(values) >= 4 else None


def device_power(intervals: dict[str, float], activity: list[dict], min_slots: int = 6,
                 known_watts: dict[str, float] | None = None, reported: set[str] | None = None) -> dict[str, dict]:
    """kW per device. Entered wattages are used as given and taken out of the meter readings first; the big loads
    (washer, dryer, pool pump, AC, plugs) are then solved by least squares over the slots where they ran. An
    estimate is kept only if it's clearly above zero (more than twice its standard error), at least MIN_KW, and
    no more than the kind could draw."""
    known_watts = known_watts or {}
    by_slot_all = activity_by_slot(activity)
    kinds = {a["device_id"]: a["kind"] for a in activity}
    if known_watts:
        intervals = dict(intervals)
        for slot, shares in by_slot_all.items():
            if slot in intervals:
                intervals[slot] -= sum(share * 0.25 * known_watts[d] / 1000 for d, share in shares.items() if d in known_watts)
    by_slot = {slot: {d: v for d, v in shares.items() if d not in known_watts and kinds.get(d) in MEASURABLE_KINDS}
               for slot, shares in by_slot_all.items()}
    by_slot = {slot: shares for slot, shares in by_slot.items() if shares}
    busy = set(by_slot)
    rows = []
    for slot, shares in by_slot.items():
        if slot not in intervals:
            continue
        idle = local_idle(intervals, busy, slot)
        if idle is None:
            continue
        rows.append((shares, intervals[slot] - idle))
    counts: dict[str, int] = defaultdict(int)
    for shares, _ in rows:
        for d, share in shares.items():
            if share >= 0.3:
                counts[d] += 1
    devices = sorted(d for d, c in counts.items() if c >= min_slots)
    if not devices:
        return {}
    index = {d: i for i, d in enumerate(devices)}
    n = len(devices)
    ata = [[0.0] * n for _ in range(n)]
    atb = [0.0] * n
    for shares, excess in rows:
        if any(d not in index for d in shares):
            continue
        for di, si in shares.items():
            atb[index[di]] += si * 0.25 * excess
            for dj, sj in shares.items():
                ata[index[di]][index[dj]] += si * 0.25 * sj * 0.25
    for i in range(n):
        ata[i][i] += 1e-6  # keeps nearly-identical schedules solvable without changing real answers
    out = {d: {"kw": round(w / 1000, 4), "slots": 0, "source": "entered"} for d, w in known_watts.items()}
    for d in reported or {}:
        if d in out:
            out[d]["source"] = "reported"
    solution = _solve(ata, atb)
    if solution is None:
        return out
    used = [(shares, excess) for shares, excess in rows if all(d in index for d in shares)]
    dof = len(used) - n
    sse = sum((excess - sum(s * 0.25 * solution[index[d]] for d, s in shares.items())) ** 2 for shares, excess in used)
    variance = sse / dof if dof > 0 else None
    for d, kw in zip(devices, solution):
        se = None
        if variance is not None:
            unit = [0.0] * n
            unit[index[d]] = 1.0
            inv_col = _solve([row[:] for row in ata], unit)
            se = (variance * inv_col[index[d]]) ** 0.5 if inv_col and inv_col[index[d]] > 0 else None
        if kw < MIN_KW or kw > MAX_KW.get(kinds.get(d), 15.0) or (se is not None and kw <= 2 * se):
            continue
        out[d] = {"kw": round(kw, 2), "slots": counts[d], "source": "measured", "se": round(se, 2) if se is not None else None}
    return out


def degree_days(tmean: float | None) -> tuple[float, float]:
    if tmean is None:
        return 0.0, 0.0
    return max(tmean - BALANCE_F, 0.0), max(BALANCE_F - tmean, 0.0)


def hvac_model(daily_rest: dict[str, float], tmean: dict[str, float]) -> dict | None:
    """Fit daily kWh = base + a·CDD + b·HDD. Needs 10+ days with some spread in temperature."""
    rows = [(1.0, *degree_days(tmean[d]), kwh) for d, kwh in daily_rest.items() if tmean.get(d) is not None]
    if len(rows) < 10:
        return None
    cols = [[r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]]
    y = [r[3] for r in rows]
    use = [i for i in range(3) if any(cols[i])]  # drop HDD (or CDD) when it never happens in the window
    ata = [[sum(a * b for a, b in zip(cols[i], cols[j])) for j in use] for i in use]
    atb = [sum(a * b for a, b in zip(cols[i], y)) for i in use]
    for k in range(len(use)):
        ata[k][k] += 1e-6
    sol = _solve(ata, atb)
    if sol is None:
        return None
    coef = dict(zip(use, sol))
    base, a, b = coef.get(0, 0.0), max(coef.get(1, 0.0), 0.0), max(coef.get(2, 0.0), 0.0)
    pred = [base + a * r[1] + b * r[2] for r in rows]
    mean = sum(y) / len(y)
    ss_tot = sum((v - mean) ** 2 for v in y) or 1.0
    r2 = 1 - sum((v - p) ** 2 for v, p in zip(y, pred)) / ss_tot
    return {"base_kwh": round(base, 1), "per_cdd": round(a, 2), "per_hdd": round(b, 2), "r2": round(r2, 2), "days": len(rows)}


def always_on_watts(intervals: dict[str, float]) -> float | None:
    lows = []
    for day in sorted({s[:10] for s in intervals}):
        night = [intervals[f"{day}T{h:02d}:{m:02d}"] for h in range(1, 5) for m in (0, 15, 30, 45)
                 if f"{day}T{h:02d}:{m:02d}" in intervals]
        if len(night) >= 8:
            lows.append(min(night) * 4 * 1000)
    return round(statistics.median(lows)) if len(lows) >= 3 else None


def spikes(intervals: dict[str, float], activity: list[dict], names: dict[str, str], limit: int = 8) -> list[dict]:
    """Runs of 15-minute slots far above normal, with what was running."""
    if len(intervals) < 96:
        return []
    kw = {s: v * 4 for s, v in intervals.items()}
    values = sorted(kw.values())
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values) or 0.1
    threshold = max(3.0, med + 6 * mad)
    by_slot = activity_by_slot(activity)
    events: list[dict] = []
    for s in sorted(kw):
        if kw[s] < threshold:
            continue
        t = datetime.fromisoformat(s)
        last = events[-1] if events else None
        if last and datetime.fromisoformat(last["end"]) == t:
            last["end"] = slot_key(t + SLOT)
            last["peak_kw"] = max(last["peak_kw"], round(kw[s], 1))
            last["kwh"] = round(last["kwh"] + intervals[s], 1)
            last["running"] |= set(by_slot.get(s, {}))
        else:
            events.append({"start": s, "end": slot_key(t + SLOT), "peak_kw": round(kw[s], 1),
                           "kwh": round(intervals[s], 1), "running": set(by_slot.get(s, {}))})
    events.sort(key=lambda e: -e["peak_kw"])
    for e in events:
        e["running"] = sorted(names.get(d, d) for d in e["running"])
    return events[:limit]


def build_energy(store: EnergyStore, tmean: dict[str, float], days: int = 28, today: date | None = None) -> dict:
    today = today or date.today()
    since = today - timedelta(days=days)
    intervals = {s: v for s, v in store.intervals(since).items() if s[:10] < today.isoformat()}
    activity = store.activity(since)
    names = {a["device_id"]: a["name"] for a in activity}
    kinds = {a["device_id"]: a["kind"] for a in activity}
    entered = store.watts()
    reported = store.reported()
    estimated = store.estimated()
    known = {**{d: e["watts"] for d, e in estimated.items()}, **reported, **entered}
    power = device_power(intervals, activity, known_watts=known, reported=set(reported) - set(entered))
    for d in set(estimated) - set(reported) - set(entered):
        if d in power:
            power[d]["source"] = "rated"
    by_slot = activity_by_slot(activity)

    daily_total: dict[str, float] = defaultdict(float)
    for s, v in intervals.items():
        daily_total[s[:10]] += v
    daily_devices: dict[str, float] = defaultdict(float)
    device_kwh: dict[str, float] = defaultdict(float)
    device_hours: dict[str, float] = defaultdict(float)
    for s, shares in by_slot.items():
        for d, share in shares.items():
            device_hours[d] += share * 0.25
            if d in power and s in intervals:
                kwh = share * 0.25 * power[d]["kw"]
                daily_devices[s[:10]] += kwh
                device_kwh[d] += kwh
    rest = {d: max(daily_total[d] - daily_devices.get(d, 0), 0) for d in daily_total}
    model = hvac_model(rest, tmean)
    series = []
    for d in sorted(daily_total):
        cdd, hdd = degree_days(tmean.get(d))
        hvac = min(rest[d], model["per_cdd"] * cdd + model["per_hdd"] * hdd) if model else None
        series.append({"day": d, "total_kwh": round(daily_total[d], 1), "devices_kwh": round(daily_devices.get(d, 0), 1),
                       "hvac_kwh": round(hvac, 1) if hvac is not None else None,
                       "other_kwh": round(rest[d] - (hvac or 0), 1), "tmean_f": tmean.get(d)})
    total = sum(r["total_kwh"] for r in series)
    hvac_total = sum(r["hvac_kwh"] or 0 for r in series)
    standby = always_on_watts(intervals)
    def status(d: str) -> str:
        if d in power:
            return power[d]["source"]
        if d in estimated:
            return "rated"
        if kinds.get(d) not in MEASURABLE_KINDS:
            return "small"        # too small for the meter to see; enter watts if it's a big one
        return "learning"
    devices = sorted(({"id": d, "name": names.get(d, d), "kind": kinds.get(d), "kw": power.get(d, {}).get("kw"),
                       "hours": round(device_hours[d], 1), "kwh": round(device_kwh[d], 1) if d in power else None,
                       "status": status(d), "watts": entered.get(d), "reported_w": round(reported[d]) if d in reported else None,
                       "profile": estimated[d]["profile"] if d in estimated and status(d) == "rated" else None,
                       "profile_source": estimated[d]["source"] if d in estimated and status(d) == "rated" else None}
                      for d in device_hours), key=lambda x: -(x["kwh"] or 0))
    found_spikes = spikes(intervals, activity, names)
    recs = []
    if model and model["r2"] >= 0.4 and hvac_total:
        recs.append({"level": "good", "title": f"Heating and cooling: about {hvac_total / total:.0%} of electricity",
                     "text": f"Each degree-day adds about {max(model['per_cdd'], model['per_hdd']):.1f} kWh "
                             f"(fit to {model['days']} days, R² {model['r2']}). A thermostat 2 °F closer to outside usually saves 5–10%."})
    elif series:
        recs.append({"level": "warning", "title": "HVAC can't be separated yet",
                     "text": "It needs more days with different temperatures, or the Lennox thermostat connected."})
    if standby:
        recs.append({"level": "warning" if standby > 400 else "good",
                     "title": f"Always-on load: about {standby:,} W",
                     "text": f"That's {standby * 24 * 365 / 1000:,.0f} kWh a year before anything is switched on: "
                             "fridges, network gear, the pool pump if it runs at night, standby electronics."})
    top = [x for x in devices if x["kwh"]][:3]
    if top and total:
        recs.append({"level": "good", "title": "Biggest tracked users",
                     "text": "; ".join(f"{x['name']}: {x['kwh']:,.0f} kWh at about {x['kw']} kW" for x in top) + "."})
    unexplained = [e for e in found_spikes if not e["running"]]
    if unexplained:
        recs.append({"level": "warning", "title": f"{len(unexplained)} spike(s) with nothing tracked running",
                     "text": "Likely the AC or furnace starting, an oven, or a water heater. Connecting the Lennox thermostat would explain them."})
    missing = [x for x in devices if x["status"] == "learning" and x["hours"] >= 1]
    if missing:
        recs.append({"level": "warning", "title": "Still learning some devices",
                     "text": "Power isn't known yet for " + ", ".join(x["name"] for x in missing[:4]) +
                             ". It needs about 6 runs that line up with the meter's 15-minute data (a day late), and stays "
                             "unknown if the device uses too little to stand out. You can enter its watts instead."})
    bulbs = list(estimated.values())
    if bulbs:
        standby_w = sum(b["standby"] for b in bulbs)
        light_kwh = sum(x["kwh"] or 0 for x in devices if x["status"] == "rated")
        recs.append({"level": "good", "title": f"Smart lights: about {light_kwh:,.0f} kWh in {days} days",
                     "text": f"Estimated from each bulb's model and the brightness it was used at ({len(bulbs)} bulbs). "
                             f"Switched off they still draw about {standby_w:.0f} W together, {standby_w * 8.76:,.0f} kWh a year, "
                             "because they stay connected to the network."})
    small = [x for x in devices if x["status"] == "small"]
    if small:
        recs.append({"level": "good", "title": "Lights and small devices aren't guessed",
                     "text": "Dimmers, TVs, purifiers and robot vacuums use too little for the meter to measure and don't "
                             "say what's connected, so the hub doesn't estimate them. If one runs a big load, such as an "
                             "incandescent chandelier on a dimmer, enter its watts in the device list."})
    return {"days": days, "series": series, "devices": devices, "spikes": found_spikes, "hvac": model,
            "standby_w": standby, "recommendations": recs,
            "summary": {"total_kwh": round(total), "hvac_kwh": round(hvac_total) if model else None,
                        "devices_kwh": round(sum(r["devices_kwh"] for r in series)),
                        "peak_kw": max((v * 4 for v in intervals.values()), default=None)},
            "has_energy": bool(intervals), "has_activity": bool(activity)}
