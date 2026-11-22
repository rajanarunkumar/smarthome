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

    def observe(self, devices, now: datetime) -> None:
        """Record running periods from a device snapshot (called every couple of minutes)."""
        seen = set()
        for d in devices:
            if d.kind not in TRACKED_KINDS or not d.online:
                continue
            if d.on is not True:
                continue
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


def device_power(intervals: dict[str, float], activity: list[dict], min_slots: int = 6) -> dict[str, dict]:
    """kW per tracked device, by least squares over slots where devices ran."""
    by_slot = activity_by_slot(activity)
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
    solution = _solve(ata, atb)
    if solution is None:
        return {}
    return {d: {"kw": round(kw, 2), "slots": counts[d]} for d, kw in zip(devices, solution) if kw > 0.05}


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
    power = device_power(intervals, activity)
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
    devices = sorted(({"id": d, "name": names.get(d, d), "kind": kinds.get(d), "kw": power.get(d, {}).get("kw"),
                       "hours": round(device_hours[d], 1), "kwh": round(device_kwh[d], 1) if d in power else None}
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
    missing = [x for x in devices if x["kwh"] is None and x["hours"] >= 1]
    if missing:
        recs.append({"level": "warning", "title": "Still learning some devices",
                     "text": "Power isn't known yet for " + ", ".join(x["name"] for x in missing[:4]) +
                             ". It needs about 6 runs that line up with the meter's 15-minute data, which arrives a day late."})
    return {"days": days, "series": series, "devices": devices, "spikes": found_spikes, "hvac": model,
            "standby_w": standby, "recommendations": recs,
            "summary": {"total_kwh": round(total), "hvac_kwh": round(hvac_total) if model else None,
                        "devices_kwh": round(sum(r["devices_kwh"] for r in series)),
                        "peak_kw": max((v * 4 for v in intervals.values()), default=None)},
            "has_energy": bool(intervals), "has_activity": bool(activity)}
