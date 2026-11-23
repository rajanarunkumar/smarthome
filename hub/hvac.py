"""Heating and cooling: run time, cost by free/paid hours, and health from the thermostats' own state.

Every 2 minutes the hub notes each Nest or Lennox thermostat's state: heating, cooling or idle,
indoor temperature, humidity and set temperatures. From that:

- Runs: when the system started and stopped, how long it ran, and whether it reached its set
  temperature. Minutes are split into TXU's free window and paid hours.
- kW: the meter (Smart Meter Texas, 15-minute kWh) is matched against the runs to learn what the
  system draws (hub/energy.py device_power), so run time becomes kWh and dollars at the bill's prices.
- Health (deterministic, same idea as the pool pump): minutes of cooling per cooling degree-day
  (heating per heating degree-day) against the system's own clean baseline. A dirty filter or coil
  makes it run longer for the same weather. Plus: runs that can't reach the set temperature,
  short cycling, two thermostats heating and cooling at once, and humid air while cooling.
- A log of fixes (filter, coil, service, schedule) with before/after run time per degree-day.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from .pool import in_free

SAMPLE_MIN = 2              # the recorder's interval
GAP_MIN = 6                 # samples further apart than this are a gap in the data
MIN_DD = 3.0                # days milder than this say little about efficiency
FULL_DAY_SAMPLES = 360      # half a day of readings: enough to count a day's run time
SHORT_RUN_MIN = 5
LONG_RUN_MIN = 120
WATCH, ACT, URGENT = 1.15, 1.25, 1.40
BALANCE_F = 65.0

ACTIONS = {
    "filter": ("Replace the air filter", "Replaced the air filter"),
    "coil": ("Hose off the outdoor unit's coil (power off at the disconnect first)", "Cleaned the outdoor coil"),
    "service": ("Book an HVAC technician: refrigerant charge and airflow check", "HVAC service visit"),
    "schedule": ("Change the thermostat schedule", "Changed the thermostat schedule"),
}
ACTIVE = {"HEATING": "heat", "COOLING": "cool"}


class HvacStore:
    def __init__(self, db) -> None:
        self.db = db
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hvac_samples (
                device_id TEXT NOT NULL, t TEXT NOT NULL, name TEXT, hvac TEXT, mode TEXT,
                indoor_f REAL, humidity REAL, heat_f REAL, cool_f REAL, PRIMARY KEY (device_id, t));
            CREATE INDEX IF NOT EXISTS hvac_samples_t ON hvac_samples(t);
            CREATE TABLE IF NOT EXISTS hvac_maintenance (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, device_id TEXT, note TEXT);
            """
        )
        db.commit()

    def record(self, device_id: str, name: str, th: dict, now: datetime) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO hvac_samples (device_id, t, name, hvac, mode, indoor_f, humidity, heat_f, cool_f) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, now.isoformat(timespec="seconds"), name, th.get("hvac") or "OFF", th.get("mode"),
             th.get("current"), th.get("humidity"), th.get("heat"), th.get("cool")))
        self.db.commit()

    def record_devices(self, devices, now: datetime) -> int:
        n = 0
        for d in devices:
            if d.kind == "thermostat" and d.online and d.thermostat:
                self.record(d.id, d.name, d.thermostat, now)
                n += 1
        return n

    def samples(self, since: datetime) -> list[dict]:
        cur = self.db.execute("SELECT device_id, t, name, hvac, mode, indoor_f, humidity, heat_f, cool_f FROM hvac_samples "
                              "WHERE t >= ? ORDER BY device_id, t", (since.isoformat(timespec="seconds"),))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def log(self, action: str, at: datetime, device_id: str | None = None, note: str | None = None) -> dict:
        cur = self.db.execute("INSERT INTO hvac_maintenance (at, action, device_id, note) VALUES (?, ?, ?, ?)",
                              (at.isoformat(timespec="minutes"), action, device_id, note))
        self.db.commit()
        return {"id": cur.lastrowid, "at": at.isoformat(timespec="minutes"), "action": action, "device_id": device_id, "note": note}

    def delete_log(self, log_id: int) -> None:
        self.db.execute("DELETE FROM hvac_maintenance WHERE id = ?", (log_id,))
        self.db.commit()

    def maintenance(self) -> list[dict]:
        cur = self.db.execute("SELECT id, at, action, device_id, note FROM hvac_maintenance ORDER BY at")
        return [{"id": r[0], "at": r[1], "action": r[2], "device_id": r[3], "note": r[4]} for r in cur.fetchall()]


# Runs
def runs(samples: list[dict], window: tuple[int, int]) -> list[dict]:
    """Contiguous heating or cooling periods per thermostat. `clipped` runs touch a gap in the data,
    so their length and end aren't known exactly; they count toward minutes but not toward cycling."""
    by_dev: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_dev[s["device_id"]].append(s)
    out = []
    for dev, rows in by_dev.items():
        cur = None
        prev_t = None
        for s in rows:
            t = datetime.fromisoformat(s["t"])
            kind = ACTIVE.get(s["hvac"])
            gap = prev_t is not None and (t - prev_t).total_seconds() > GAP_MIN * 60
            if cur and (gap or kind != cur["kind"]):
                end = prev_t + timedelta(minutes=SAMPLE_MIN) if gap else t
                _close(cur, end, window, clipped=gap)
                out.append(cur)
                cur = None
            if kind and cur is None:
                cur = {"device_id": dev, "name": s["name"], "kind": kind, "start": t, "clipped": gap or prev_t is None,
                       "start_f": s["indoor_f"], "end_f": s["indoor_f"], "set_f": None, "hum": [], "slices": []}
            if cur:
                cur["end_f"] = s["indoor_f"]
                cur["set_f"] = s["cool_f"] if kind == "cool" else s["heat_f"]
                if s["humidity"] is not None:
                    cur["hum"].append(s["humidity"])
                cur["slices"].append(t)
            prev_t = t
        if cur:
            _close(cur, prev_t + timedelta(minutes=SAMPLE_MIN), window, clipped=True)
            out.append(cur)
    return sorted(out, key=lambda r: r["start"])


def _close(run: dict, end: datetime, window: tuple[int, int], clipped: bool) -> None:
    run["end"] = end
    run["clipped"] = run["clipped"] or clipped
    run["minutes"] = round((end - run["start"]).total_seconds() / 60, 1)
    free = paid = 0.0
    t = run["start"]
    while t < end:
        step = min(end, t + timedelta(minutes=1)) - t
        if in_free(t, window):
            free += step.total_seconds() / 60
        else:
            paid += step.total_seconds() / 60
        t += timedelta(minutes=1)
    run["free_min"], run["paid_min"] = round(free, 1), round(paid, 1)
    run["humidity"] = statistics.median(run.pop("hum")) if run.get("hum") else None
    run.pop("slices", None)
    # Didn't get within 1 °F of the set temperature, on a long run: the system can't keep up.
    run["short_of_set"] = (run["set_f"] is not None and run["end_f"] is not None and
                           (run["end_f"] - run["set_f"] >= 2 if run["kind"] == "cool" else run["set_f"] - run["end_f"] >= 2))


def degree_days(tmean: float | None) -> tuple[float, float]:
    if tmean is None:
        return 0.0, 0.0
    return max(tmean - BALANCE_F, 0.0), max(BALANCE_F - tmean, 0.0)


def daily(run_list: list[dict], samples: list[dict], tmean: dict[str, float]) -> dict[str, list[dict]]:
    """Per thermostat, per day: minutes cooling and heating, free and paid, cycles, and run time per degree-day."""
    coverage: dict[tuple[str, str], int] = defaultdict(int)
    for s in samples:
        coverage[(s["device_id"], s["t"][:10])] += 1
    days: dict[str, dict[str, dict]] = defaultdict(dict)
    for (dev, day), n in coverage.items():
        cdd, hdd = degree_days(tmean.get(day))
        days[dev][day] = {"day": day, "cool_min": 0.0, "heat_min": 0.0, "free_min": 0.0, "paid_min": 0.0, "cycles": 0,
                          "short": 0, "samples": n, "cdd": round(cdd, 1), "hdd": round(hdd, 1), "tmean_f": tmean.get(day)}
    for r in run_list:
        d = days[r["device_id"]].get(r["start"].date().isoformat())
        if d is None:
            continue
        d[f"{r['kind']}_min"] += r["minutes"]
        d["free_min"] += r["free_min"]
        d["paid_min"] += r["paid_min"]
        if not r["clipped"]:
            d["cycles"] += 1
            d["short"] += r["minutes"] < SHORT_RUN_MIN
    out = {}
    for dev, by_day in days.items():
        rows = []
        for day in sorted(by_day):
            d = by_day[day]
            full = d["samples"] >= FULL_DAY_SAMPLES
            d["per_cdd"] = round(d["cool_min"] / d["cdd"], 1) if full and d["cdd"] >= MIN_DD and d["cool_min"] else None
            d["per_hdd"] = round(d["heat_min"] / d["hdd"], 1) if full and d["hdd"] >= MIN_DD and d["heat_min"] else None
            for k in ("cool_min", "heat_min", "free_min", "paid_min"):
                d[k] = round(d[k], 1)
            rows.append(d)
        out[dev] = rows
    return out


# Health
def baseline(rows: list[dict], key: str, fixes: list[dict]) -> tuple[float | None, str | None]:
    """The system's clean level: the 25th percentile of its days, or the 3 days after the last filter,
    coil or service fix if that was lower (a fix that didn't help doesn't become the baseline)."""
    vals = [(r["day"], r[key]) for r in rows if r[key] is not None]
    if len(vals) < 5:
        return None, None
    q = sorted(v for _, v in vals)[len(vals) // 4]
    best, src = q, "your best quarter of days"
    for f in reversed(fixes):
        if f["action"] in ("filter", "coil", "service"):
            day0 = f["at"][:10]
            after = [v for d, v in vals if d > day0][:3]
            if len(after) >= 2 and statistics.median(after) < best:
                best, src = statistics.median(after), f"the days after {ACTIONS[f['action']][1].lower()}"
            break
    return best, src


def diagnose(rows: list[dict], dev_runs: list[dict], fixes: list[dict], today: date) -> dict:
    """One thermostat: level, evidence and the actions to take, in order."""
    mode = "cool" if sum(r["cool_min"] for r in rows) >= sum(r["heat_min"] for r in rows) else "heat"
    key = "per_cdd" if mode == "cool" else "per_hdd"
    dd = "cooling" if mode == "cool" else "heating"
    base, src = baseline(rows, key, fixes)
    recent = [r[key] for r in rows if r[key] is not None][-3:]
    ratio = round(statistics.median(recent) / base, 2) if base and recent else None
    evidence, actions, level = [], [], "good"
    week = [r for r in dev_runs if r["start"].date() >= today - timedelta(days=7)]

    def act(*codes):
        for c in codes:
            if c not in actions:
                actions.append(c)

    if ratio is None:
        level = "learning"
        need = 5 - len([r for r in rows if r[key] is not None])
        evidence.append(f"Needs {max(need, 1)} more day(s) with {dd} weather (a mean outdoor temperature at least "
                        f"{MIN_DD:.0f} °F {'above' if mode == 'cool' else 'below'} {BALANCE_F:.0f} °F) and the thermostat online all day.")
    else:
        evidence.append(f"{dd.capitalize()} ran {statistics.median(recent):.0f} min per degree-day lately vs {base:.0f} "
                        f"for {src} ({ratio:.2f}×).")
        if ratio >= WATCH:
            level = "serious" if ratio >= URGENT else "warning"
            last_filter = max((f["at"][:10] for f in fixes if f["action"] == "filter"), default=None)
            stale = last_filter is None or (today - date.fromisoformat(last_filter)).days > 60
            if stale:
                evidence.append("No filter change logged yet." if last_filter is None
                                else f"The last filter change logged was {last_filter}, over 60 days ago.")
                act("filter", "coil", "service")
            else:
                evidence.append(f"The filter was changed on {last_filter} and run time is still high: the coil or refrigerant is next.")
                act("coil", "service")
            if ratio < ACT:
                evidence.append("A small rise: watch another few days before spending on a technician.")
    long_short = [r for r in week if not r["clipped"] and r["minutes"] >= LONG_RUN_MIN and r["short_of_set"]]
    if long_short:
        level = "serious" if len(long_short) >= 3 else max(level, "warning", key=_rank)
        r = long_short[-1]
        evidence.append(f"{len(long_short)} run(s) this week went {LONG_RUN_MIN // 60}+ hours without reaching the set temperature "
                        f"(latest: {r['minutes']:.0f} min, ended at {r['end_f']:.0f} °F vs {r['set_f']:.0f} °F).")
        act("filter", "coil", "service")
    complete = [r for r in week if not r["clipped"]]
    short = [r for r in complete if r["minutes"] < SHORT_RUN_MIN]
    if len(complete) >= 10 and len(short) / len(complete) > 0.3:
        level = max(level, "warning", key=_rank)
        evidence.append(f"Short cycling: {len(short)} of {len(complete)} runs this week were under {SHORT_RUN_MIN} minutes. "
                        "Starts wear the compressor and waste power; check the thermostat isn't near a supply vent or in sun.")
        act("service")
    hum = [r["humidity"] for r in week if r["kind"] == "cool" and r["humidity"] is not None]
    if len(hum) >= 5 and statistics.median(hum) >= 60:
        level = max(level, "warning", key=_rank)
        evidence.append(f"Indoor humidity while cooling is {statistics.median(hum):.0f}% (aim for under 55%). "
                        "Fan set to On instead of Auto, or an oversized system, leaves water in the air.")
    if level == "good" and ratio is not None:
        evidence.append("Run time per degree-day is at its clean level.")
    return {"mode": mode, "level": level, "ratio": ratio, "baseline": round(base, 1) if base else None, "baseline_from": src,
            "evidence": evidence,
            "actions": [{"action": a, "label": ACTIONS[a][0]} for a in actions]}


def _rank(level: str) -> int:
    return {"learning": 0, "good": 1, "warning": 2, "serious": 3}[level]


def fighting(run_list: list[dict]) -> float:
    """Minutes one thermostat heated while another cooled."""
    heat = [r for r in run_list if r["kind"] == "heat"]
    cool = [r for r in run_list if r["kind"] == "cool"]
    total = 0.0
    for h in heat:
        for c in cool:
            if h["device_id"] != c["device_id"]:
                total += max((min(h["end"], c["end"]) - max(h["start"], c["start"])).total_seconds() / 60, 0)
    return round(total, 1)


def fix_effect(rows: list[dict], fix: dict, key: str) -> dict | None:
    """Run time per degree-day in the 5 qualifying days before a fix and after it."""
    day0 = fix["at"][:10]
    before = [r[key] for r in rows if r[key] is not None and r["day"] < day0][-5:]
    after = [r[key] for r in rows if r[key] is not None and r["day"] > day0][:5]
    if len(before) < 2 or len(after) < 2:
        return None
    b, a = statistics.median(before), statistics.median(after)
    return {"before": round(b, 1), "after": round(a, 1), "change_pct": round((a / b - 1) * 100, 1), "days": [len(before), len(after)]}


# Report
def build_hvac(store: HvacStore, thermostats: list[dict], tariff: dict, tmean: dict[str, float], kw: dict[str, float],
               now: datetime, days: int = 14) -> dict:
    window = (int(tariff.get("free_start", 20)), int(tariff.get("free_end", 4)))
    energy_c, delivery_c = tariff.get("energy_cents"), tariff.get("delivery_cents")
    night_c = tariff.get("night_delivery_cents", delivery_c)
    fees = 1 + (tariff.get("fees_pct") or 0) / 100
    priced = energy_c is not None and delivery_c is not None
    # Health needs more history than the view shows: the baseline comes from up to 60 days.
    samples = store.samples(now - timedelta(days=60))
    run_list = runs(samples, window)
    per_day = daily(run_list, samples, tmean)
    fixes = store.maintenance()
    since = (now - timedelta(days=days)).date().isoformat()
    names = {t["id"]: t["name"] for t in thermostats}
    for s in samples:
        names.setdefault(s["device_id"], s["name"])
    systems, recs = [], []
    for dev in sorted(names):
        rows = per_day.get(dev, [])
        dev_runs = [r for r in run_list if r["device_id"] == dev]
        mine = [f for f in fixes if f["device_id"] in (None, dev)]
        health = diagnose(rows, dev_runs, mine, now.date())
        shown = [r for r in rows if r["day"] >= since]
        n = len(shown) or 1
        free_min = sum(r["free_min"] for r in shown)
        paid_min = sum(r["paid_min"] for r in shown)
        k = kw.get(dev)
        cost = None
        if k and priced:
            free_usd = free_min / 60 * k * night_c * fees / 100
            paid_usd = paid_min / 60 * k * (energy_c + delivery_c) * fees / 100
            cost = {"free": round(free_usd, 2), "paid": round(paid_usd, 2), "per_month": round((free_usd + paid_usd) / n * 30, 2)}
        key = "per_cdd" if health["mode"] == "cool" else "per_hdd"
        log = [{**f, "label": ACTIONS.get(f["action"], (f["action"], f["action"]))[1], "effect": fix_effect(rows, f, key)}
               for f in mine]
        live = next((t for t in thermostats if t["id"] == dev), None)
        lead = _hour_minutes(dev_runs, since, [(window[0] - i) % 24 for i in (1, 2, 3, 4)])
        trail = _hour_minutes(dev_runs, since, [(window[1] + i) % 24 for i in (0, 1, 2, 3)])
        systems.append({"id": dev, "name": names[dev], "live": live, "health": health, "kw": k,
                        "days": shown, "free_min": round(free_min), "paid_min": round(paid_min), "cost": cost,
                        "lead_min_day": round(lead / n), "trail_min_day": round(trail / n), "maintenance": log,
                        "runs_week": len([r for r in dev_runs if r["start"] >= now - timedelta(days=7)])})
        recs += _tou_recs(names[dev], health["mode"], shown, lead / n, trail / n, k, tariff, priced, window, fees)
    fight = fighting([r for r in run_list if r["start"].date().isoformat() >= since])
    if fight >= 10:
        recs.insert(0, {"level": "serious", "title": f"Heating and cooling ran at the same time for {fight:.0f} minutes",
                        "text": "Two thermostats worked against each other. Put both on the same mode, or widen the gap between "
                                "one zone's heat setting and the other's cool setting to at least 4 °F."})
    return {"window": {"start": window[0], "end": window[1]}, "systems": systems, "recommendations": recs, "priced": priced,
            "samples": len(samples), "first_sample": samples[0]["t"] if samples else None,
            "actions": [{"action": a, "label": v[1]} for a, v in ACTIONS.items()]}


def _hour_minutes(dev_runs: list[dict], since: str, hours: list[int]) -> float:
    total = 0.0
    for r in dev_runs:
        if r["start"].date().isoformat() < since:
            continue
        t = r["start"]
        while t < r["end"]:
            if t.hour in hours:
                total += (min(r["end"], t + timedelta(minutes=1)) - t).total_seconds() / 60
            t += timedelta(minutes=1)
    return total


def _h(hour: int) -> str:
    return f"{(hour % 12) or 12} {'AM' if hour % 24 < 12 else 'PM'}"


def _tou_recs(name: str, mode: str, rows: list[dict], lead: float, trail: float, k: float | None, tariff: dict,
              priced: bool, window: tuple[int, int], fees: float) -> list[dict]:
    out = []
    if not rows:
        return out
    n = len(rows)
    free = sum(r["free_min"] for r in rows)
    paid = sum(r["paid_min"] for r in rows)
    if free + paid == 0:
        return out
    energy_c, delivery_c = tariff.get("energy_cents") or 0, tariff.get("delivery_cents") or 0
    night_c = tariff.get("night_delivery_cents", delivery_c) or 0
    rate = f" at {k:.1f} kW" if k else ""

    def money(minutes: float, cents: float, lead: str) -> str:
        return f" {lead} ${minutes / 60 * k * cents * fees / 100 * 30:.0f} a month." if k and priced else ""
    out.append({"level": "good" if paid <= free else "warning",
                "title": f"{name}: {paid / (free + paid):.0%} of run time is in paid hours",
                "text": f"{paid / n / 60:.1f} h a day paid, {free / n / 60:.1f} h a day free{rate}."
                        + money(paid / n, energy_c + delivery_c, "Paid hours cost about")
                        + ("" if k else " The kW comes from the meter once a few runs line up with it.")})
    if mode == "cool" and lead >= 20:
        out.append({"level": "warning",
                    "title": f"{name}: cooling runs {lead:.0f} min a day in the 4 hours before {_h(window[0])}",
                    "text": f"Raise the cooling setting 2–3 °F from {_h((window[0] - 4) % 24)} and bring it back down at {_h(window[0])}, "
                            "when energy is free. The house cools back in the free hours."
                            + money(lead, energy_c + delivery_c - night_c, "Moving them saves up to")})
    if mode == "heat" and trail >= 20:
        out.append({"level": "warning",
                    "title": f"{name}: heating runs {trail:.0f} min a day in the 4 hours after {_h(window[1])}",
                    "text": f"Warm the house to its morning temperature before {_h(window[1])} (free energy), and set heat "
                            f"2 °F lower from {_h(window[1])} to 8 AM."
                            + money(trail, energy_c + delivery_c - night_c, "Moving them saves up to")})
    if mode == "cool" and free and priced and k:
        out.append({"level": "good", "title": f"{name}: night cooling still costs delivery",
                    "text": f"{free / n / 60:.1f} h a night × {k:.1f} kW × {night_c:.1f}¢ delivery."
                            + money(free / n, night_c, "About") + " A degree warmer at night trims it."})
    return out
