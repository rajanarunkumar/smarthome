"""Pool pump health and running cost, from the IntelliFlo VSF's own readings.

Every 2 minutes the hub records the pump's flow (GPM), power (W), motor speed (%),
its estimated pressure, and the target it is holding. In flow mode the pump holds
the target GPM, so when something restricts the water it has to spin faster and
draw more power for the same flow. That extra power at the same GPM is the
evidence used here; nothing is guessed:

- Baseline: for each target flow, the median power in the 48 hours after the
  filter was last backwashed or cleaned (logged in the app). Before any such log,
  the lowest daily median of the last 30 days (the cleanest the system has been).
- Steady readings only: running, holding the target within 1.5 GPM, same target
  as the previous reading (skips priming and ramping).
- Power ratio = recent median ÷ baseline, per target, combined by sample count.

What the shape of the rise points to (the cheapest check first):
- Jump within a day: debris in the skimmer or pump basket (a).
- Can't hold the target flow, or power jumping around: the pump is starving or
  sucking air, usually low water (d), then the skimmer basket (a).
- Steady climb over days: the filter is loading: backwash (b). If a backwash in
  the last week didn't bring it back, the filter itself needs cleaning (c).

Logging each fix gives before/after evidence of what actually worked.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

STEADY_GPM = 1.5          # within this of the target counts as holding it
MAX_GAP_S = 300           # a longer gap between readings isn't counted as running time
WATCH, ACT, URGENT = 1.08, 1.15, 1.30
JUMP = 0.08               # day-over-day rise that counts as sudden
ERRATIC_CV = 0.06         # power spread while holding one target
MIN_STEADY = 10           # readings needed for a baseline or a recent median
ACTIONS = {
    "skimmer": "Emptied skimmer basket",
    "backwash": "Ran backwash",
    "filter_clean": "Cleaned the filter",
    "water_fill": "Topped up the water",
}
FILTER_RESET = ("backwash", "filter_clean")


class PoolStore:
    def __init__(self, db) -> None:
        self.db = db
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pool_samples (
                t TEXT PRIMARY KEY,            -- local time 'YYYY-MM-DDTHH:MM:SS'
                gpm REAL, watts REAL, speed_pct REAL, pressure_psi REAL,
                target REAL, target_kind TEXT, program INTEGER, running INTEGER NOT NULL,
                alarm INTEGER, device_time TEXT);
            CREATE TABLE IF NOT EXISTS pool_maintenance (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, note TEXT);
            """
        )
        db.commit()
        self._last_device_time: str | None = None

    def record(self, reading: dict, now: datetime) -> bool:
        """Store one reading, at the pump's own time when it has one. Skips repeats of a reading
        already stored (Pentair's cloud not updated yet)."""
        dt = reading.get("device_time")
        if dt and dt == self._last_device_time:
            return False
        own = reading_time(dt)
        if own is not None and abs((now - own).total_seconds()) < 6 * 3600:
            if self.db.execute("SELECT 1 FROM pool_samples WHERE device_time = ?", (dt,)).fetchone():
                self._last_device_time = dt
                return False
            now = own
        self._last_device_time = dt
        self.db.execute(
            "INSERT OR REPLACE INTO pool_samples (t, gpm, watts, speed_pct, pressure_psi, target, target_kind, "
            "program, running, alarm, device_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now.isoformat(timespec="seconds"), reading.get("gpm"), reading.get("watts"), reading.get("speed_pct"),
             reading.get("pressure_psi"), reading.get("target"), reading.get("target_kind"), reading.get("program"),
             int(bool(reading.get("running"))), reading.get("alarm"), dt))
        self.db.commit()
        return True

    def samples(self, since: datetime) -> list[dict]:
        cur = self.db.execute("SELECT * FROM pool_samples WHERE t >= ? ORDER BY t", (since.isoformat(timespec="seconds"),))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def log(self, action: str, at: datetime, note: str | None = None) -> dict:
        cur = self.db.execute("INSERT INTO pool_maintenance (at, action, note) VALUES (?, ?, ?)",
                              (at.isoformat(timespec="minutes"), action, note))
        self.db.commit()
        return {"id": cur.lastrowid, "at": at.isoformat(timespec="minutes"), "action": action, "note": note}

    def delete_log(self, log_id: int) -> None:
        self.db.execute("DELETE FROM pool_maintenance WHERE id = ?", (log_id,))
        self.db.commit()

    def maintenance(self, since: datetime | None = None) -> list[dict]:
        cur = self.db.execute("SELECT id, at, action, note FROM pool_maintenance WHERE at >= ? ORDER BY at",
                              ((since or datetime(2000, 1, 1)).isoformat(timespec="minutes"),))
        return [{"id": r[0], "at": r[1], "action": r[2], "note": r[3]} for r in cur.fetchall()]


STALE_MIN = 10            # a reading older than this isn't "live"


def reading_time(device_time: str | None) -> datetime | None:
    """The pump's own clock (local time), 'YYMMDDHHmmSS'."""
    try:
        return datetime.strptime(str(device_time), "%y%m%d%H%M%S")
    except (TypeError, ValueError):
        return None


def age_minutes(reading: dict | None, now: datetime) -> float | None:
    t = reading_time((reading or {}).get("device_time"))
    return None if t is None else round((now - t).total_seconds() / 60, 1)


# ----- reading the pump -----
def _num(fields: dict, key: str, scale: float = 1.0) -> float | None:
    f = fields.get(key)
    v = f.get("value") if isinstance(f, dict) else f
    try:
        return round(float(v) / scale, 2)
    except (TypeError, ValueError):
        return None


def parse_reading(fields: dict) -> dict:
    """IntelliFlo VSF (IF31) fields -> one reading. Units checked against the live pump:
    s26 tenths of GPM, s18 W, s19 tenths of %, s17 hundredths of psi (the drive's estimate),
    s15 what the target is in (2 = flow), s16 the target in tenths."""
    watts = _num(fields, "s18")
    ref = _num(fields, "s15")
    kind = {2.0: "gpm"}.get(ref, "speed")
    program = _num(fields, "s14")
    raw_time = (fields.get("s1") or {}).get("value") if isinstance(fields.get("s1"), dict) else fields.get("s1")
    return {"gpm": _num(fields, "s26", 10), "watts": watts, "speed_pct": _num(fields, "s19", 10),
            "pressure_psi": _num(fields, "s17", 100), "target": _num(fields, "s16", 10), "target_kind": kind,
            "program": int(program) if program is not None else None,
            "running": bool(watts and watts > 20), "alarm": int(_num(fields, "s20") or 0), "device_time": raw_time}


def parse_programs(fields: dict) -> list[dict]:
    """The pump's own schedules (programs 1-8). Times are minutes after midnight."""
    out = []
    for n in range(1, 9):
        def g(e):
            f = fields.get(f"zp{n}e{e}")
            return f.get("value") if isinstance(f, dict) else f
        if str(g(13)) != "1":
            continue
        kind = {"0": "schedule", "1": "interval", "2": "manual"}.get(str(g(5)), "?")
        ref = str(g(3))
        try:
            value = float(g(4)) / 10
            start, end = int(g(6)), int(g(7))
        except (TypeError, ValueError):
            continue
        out.append({"number": n, "name": (g(2) or f"Program {n}").strip(), "type": kind,
                    "enabled": str(g(10)) == "1", "target": value, "target_kind": "gpm" if ref == "2" else "speed",
                    "start_min": start, "end_min": end if kind == "schedule" else (start + end) % 1440,
                    "days": int(g(8) or 0)})
    return out


# ----- time-of-use -----
def in_free(t: datetime, window: tuple[int, int]) -> bool:
    start, end = window
    h = t.hour
    return h >= start or h < end if start > end else start <= h < end


def minutes_outside(start: int, end: int, window: tuple[int, int]) -> int:
    """Minutes of a daily start..end span (minutes after midnight, may wrap) that fall outside the free window."""
    total = (end - start) % 1440 or 1440
    out = 0
    for m in range(total):
        h = ((start + m) % 1440) // 60
        if not in_free(datetime(2000, 1, 1, h), window):
            out += 1
    return out


def energy_by_day(samples: list[dict], window: tuple[int, int]) -> list[dict]:
    """kWh and running hours per day, split into the free window and paid hours.
    Each reading's power counts until the next reading (at most MAX_GAP_S)."""
    days: dict[str, dict] = defaultdict(lambda: {"free_kwh": 0.0, "paid_kwh": 0.0, "free_h": 0.0, "paid_h": 0.0, "gallons": 0.0})
    for a, b in zip(samples, samples[1:]):
        if not a["running"] or not a["watts"]:
            continue
        ta, tb = datetime.fromisoformat(a["t"]), datetime.fromisoformat(b["t"])
        dt = min((tb - ta).total_seconds(), MAX_GAP_S)
        if dt <= 0:
            continue
        d = days[a["t"][:10]]
        key = "free" if in_free(ta, window) else "paid"
        d[f"{key}_kwh"] += a["watts"] * dt / 3.6e6
        d[f"{key}_h"] += dt / 3600
        d["gallons"] += (a["gpm"] or 0) * dt / 60
    return [{"day": k, **{x: round(v, 2) for x, v in d.items()}} for k, d in sorted(days.items())]


# ----- filter health -----
def steady(samples: list[dict]) -> list[dict]:
    """Readings where the pump is settled on a flow target (not priming, ramping or changing target)."""
    out = []
    for prev, s in zip(samples, samples[1:]):
        if (s["running"] and prev["running"] and s["target_kind"] == "gpm" and s["target"] and s["gpm"] and s["watts"]
                and prev["target"] == s["target"] and abs(s["gpm"] - s["target"]) <= STEADY_GPM):
            out.append(s)
    return out


def _by_target(rows: list[dict]) -> dict[float, list[float]]:
    out: dict[float, list[float]] = defaultdict(list)
    for r in rows:
        out[round(r["target"])].append(r["watts"])
    return out


def baselines(samples: list[dict], maintenance: list[dict]) -> tuple[dict[float, dict], str]:
    """Clean-filter power per target flow, and how it was set.

    The cleanest day recorded (lowest daily median), or the 48 hours after the last
    backwash or filter clean if that was lower. A backwash that didn't help therefore
    doesn't become the new normal."""
    st = steady(samples)
    per_day: dict[float, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for s in st:
        per_day[round(s["target"])][s["t"][:10]].append(s["watts"])
    base: dict[float, dict] = {}
    how = "the cleanest day recorded"
    for t, days in per_day.items():
        medians = [statistics.median(w) for w in days.values() if len(w) >= MIN_STEADY]
        if medians:
            base[t] = {"watts": min(medians), "n": len(medians)}
    resets = [m for m in maintenance if m["action"] in FILTER_RESET]
    if resets:
        at = datetime.fromisoformat(resets[-1]["at"])
        window = [s for s in st if at <= datetime.fromisoformat(s["t"]) < at + timedelta(hours=48)]
        used = False
        for t, w in _by_target(window).items():
            if len(w) >= MIN_STEADY and (t not in base or statistics.median(w) < base[t]["watts"]):
                base[t] = {"watts": statistics.median(w), "n": len(w)}
                used = True
        if used:
            how = f"after the {ACTIONS[resets[-1]['action']].lower()} on {at:%b %-d}"
    return base, how


def ratio(rows: list[dict], base: dict[float, dict]) -> tuple[float | None, int]:
    """Power ÷ baseline across targets, weighted by readings. None if too few readings."""
    num = den = 0.0
    n = 0
    for t, w in _by_target(rows).items():
        if t in base and len(w) >= 3:
            num += statistics.median(w) / base[t]["watts"] * len(w)
            den += len(w)
            n += len(w)
    return (round(num / den, 3) if den and n >= 3 else None), n


def daily_ratio(samples: list[dict], base: dict[float, dict]) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for s in steady(samples):
        by_day[s["t"][:10]].append(s)
    out = []
    for d, rows in sorted(by_day.items()):
        r, n = ratio(rows, base)
        p = [x["pressure_psi"] for x in rows if x["pressure_psi"]]
        out.append({"day": d, "ratio": r, "n": n, "pressure_psi": round(statistics.median(p), 1) if p else None})
    return out


def maintenance_effect(samples: list[dict], base: dict[float, dict], m: dict, hours: int = 24) -> dict | None:
    """Power ratio in the day before and the day after a logged fix: the evidence of what it did."""
    at = datetime.fromisoformat(m["at"])
    st = steady(samples)
    before = [s for s in st if at - timedelta(hours=hours) <= datetime.fromisoformat(s["t"]) < at]
    after = [s for s in st if at < datetime.fromisoformat(s["t"]) <= at + timedelta(hours=hours)]
    rb, nb = ratio(before, base)
    ra, na = ratio(after, base)
    if rb is None or ra is None:
        return None
    return {"before": rb, "after": ra, "change_pct": round((ra / rb - 1) * 100, 1), "n_before": nb, "n_after": na}


def diagnose(samples: list[dict], maintenance: list[dict], now: datetime) -> dict:
    base, base_how = baselines(samples, maintenance)
    st = steady(samples)
    recent_from = now - timedelta(hours=24)
    recent = [s for s in st if datetime.fromisoformat(s["t"]) >= recent_from]
    r_now, n_now = ratio(recent, base)
    days = daily_ratio(samples, base)
    evidence: list[str] = []
    causes: list[str] = []
    level = "good"

    # Starving or sucking air: target not held while running, speed pinned, or power jumping around.
    last6 = [s for s in samples if datetime.fromisoformat(s["t"]) >= now - timedelta(hours=6) and s["running"]
             and s["target_kind"] == "gpm" and s["target"] and s["gpm"] is not None]
    short = [s for s in last6 if s["gpm"] < s["target"] - 3]
    pinned = [s for s in last6 if (s["speed_pct"] or 0) >= 98]
    if len(short) >= 3:
        evidence.append(f"Couldn't hold the target flow in {len(short)} of {len(last6)} readings in the last 6 hours "
                        f"(e.g. {short[-1]['gpm']:.0f} GPM against {short[-1]['target']:.0f}).")
        causes += ["water_fill", "skimmer"]
        level = "serious"
    if pinned:
        evidence.append(f"Motor at {max(s['speed_pct'] for s in pinned):.0f}% speed: no headroom left.")
        causes += ["water_fill", "skimmer"]
        level = "serious"
    spread = []
    for t, w in _by_target([s for s in st if datetime.fromisoformat(s["t"]) >= now - timedelta(hours=6)]).items():
        if len(w) >= 6 and statistics.mean(w):
            cv = statistics.pstdev(w) / statistics.mean(w)
            if cv > ERRATIC_CV:
                spread.append((t, cv))
    if spread:
        t, cv = max(spread, key=lambda x: x[1])
        evidence.append(f"Power swinging ±{cv:.0%} while holding {t:.0f} GPM in the last 6 hours: a sign of air in the line.")
        causes += ["water_fill", "skimmer"]
        level = "warning" if level == "good" else level

    # Extra power at the same flow.
    if r_now is not None:
        pct = (r_now - 1) * 100
        evidence.append(f"Last 24 h: {pct:+.0f}% power at the same flow vs. baseline ({n_now} steady readings).")
        if r_now >= ACT:
            level = "serious" if r_now >= URGENT else ("warning" if level == "good" else level)
            valid = [d for d in days if d["ratio"] is not None]
            jump = None
            for a, b in zip(valid, valid[1:]):
                if b["ratio"] - a["ratio"] >= JUMP:
                    jump = b
            recent_backwash = [m for m in maintenance if m["action"] == "backwash"
                               and datetime.fromisoformat(m["at"]) >= now - timedelta(days=7)]
            if jump and jump["day"] >= (now - timedelta(days=2)).date().isoformat():
                evidence.append(f"It jumped on {date.fromisoformat(jump['day']):%b %-d} (to {jump['ratio']:.2f}×), "
                                "not gradually: typical of leaves or debris.")
                causes += ["skimmer", "backwash"]
            elif recent_backwash:
                evidence.append("A backwash in the last week didn't bring it back down.")
                causes += ["filter_clean", "skimmer"]
            else:
                rising = [d for d in valid[-5:]]
                if len(rising) >= 3 and all(b["ratio"] >= a["ratio"] - 0.01 for a, b in zip(rising, rising[1:])):
                    evidence.append(f"Climbing steadily over {len(rising)} days: the filter is loading.")
                causes += ["backwash", "skimmer"]
        elif r_now >= WATCH:
            level = "warning" if level == "good" else level
            causes += ["skimmer"]

    # Pressure (the drive's own estimate) as corroboration.
    if base and recent:
        p_recent = [s["pressure_psi"] for s in recent if s["pressure_psi"]]
        p_base_rows = [d["pressure_psi"] for d in days if d["pressure_psi"]]
        if p_recent and p_base_rows:
            p_now, p_low = statistics.median(p_recent), min(p_base_rows)
            if p_low and p_now > p_low * 1.1:
                evidence.append(f"Estimated pressure {p_now:.1f} psi vs. {p_low:.1f} on the cleanest day.")

    alarms = [s for s in samples[-30:] if s.get("alarm")]
    if alarms:
        evidence.append(f"The pump reported alarm code {alarms[-1]['alarm']}.")
        level = "serious"

    seen, order = set(), []
    for c in causes:
        if c not in seen:
            seen.add(c)
            order.append(c)
    learning = not base
    return {
        "level": "learning" if learning and level == "good" else level,
        "ratio": r_now, "readings_24h": n_now,
        "baseline": {str(int(t)): round(b["watts"]) for t, b in sorted(base.items())}, "baseline_from": base_how if base else None,
        "evidence": evidence, "actions": [{"action": c, "label": ACTIONS[c]} for c in order],
        "days": days,
    }


def flow_efficiency(samples: list[dict]) -> list[dict]:
    """Measured power and gallons per kWh at each target flow (lower flow moves water far more cheaply)."""
    rows = []
    for t, w in sorted(_by_target(steady(samples)).items()):
        if len(w) >= 3:
            watts = statistics.median(w)
            rows.append({"gpm": t, "watts": round(watts), "gal_per_kwh": round(t * 60 / (watts / 1000)), "n": len(w)})
    return rows


def build_pool(store: PoolStore, programs: list[dict], live: dict | None, tariff: dict, now: datetime,
               days: int = 14) -> dict:
    window = (int(tariff.get("free_start", 20)), int(tariff.get("free_end", 4)))
    samples = store.samples(now - timedelta(days=max(days, 30)))
    maint = store.maintenance(now - timedelta(days=90))
    health = diagnose(samples, maint, now)
    energy = [d for d in energy_by_day(samples, window) if d["day"] >= (now - timedelta(days=days)).date().isoformat()]
    effects = []
    if health["baseline"]:
        base, _ = baselines(samples, maint)
        for m in maint[-10:]:
            effects.append({**m, "label": ACTIONS.get(m["action"], m["action"]), "effect": maintenance_effect(samples, base, m)})
    fees = 1 + (tariff.get("fees_pct") or 0) / 100
    paid_rate = ((tariff.get("energy_cents") or 0) + (tariff.get("delivery_cents") or 0)) * fees
    free_rate = (tariff.get("night_delivery_cents", tariff.get("delivery_cents")) or 0) * fees
    priced = bool(tariff.get("delivery_cents"))
    sched = []
    for p in programs:
        outside = minutes_outside(p["start_min"], p["end_min"], window) if p["type"] == "schedule" else None
        sched.append({**p, "paid_minutes": outside})
    findings: list[dict] = []
    for p in sched:
        if p["enabled"] and p["paid_minutes"]:
            findings.append({"level": "warning", "title": f"“{p['name']}” runs {p['paid_minutes'] / 60:.1f} h a day in paid hours",
                             "text": f"{_clock(p['start_min'])}–{_clock(p['end_min'])} at {p['target']:.0f} "
                                     f"{'GPM' if p['target_kind'] == 'gpm' else '%'}. Moving it inside "
                                     f"{_clock(window[0] * 60)}–{_clock(window[1] * 60)} makes its energy free (delivery is still charged)."})
    age = age_minutes(live, now)
    if live is not None:
        live = {**live, "as_of": reading_time(live.get("device_time")).isoformat(timespec="minutes") if reading_time(live.get("device_time")) else None,
                "age_min": age, "stale": age is not None and age > STALE_MIN}
    if live and live["stale"]:
        findings.append({"level": "warning", "title": f"No new reading from the pump for {age / 60:.1f} h" if age >= 90 else f"No new reading from the pump for {age:.0f} min",
                         "text": "Pentair's cloud still shows the last report. Check the pump's Wi-Fi, or open the Pentair Home app once, "
                                 "which usually makes it report again. Health and cost only use fresh readings."})
    if live and live.get("running") and not live["stale"] and not in_free(now, window):
        findings.append({"level": "warning", "title": f"Running now in paid hours: {live['watts']:.0f} W",
                         "text": f"{live['gpm'] or 0:.0f} GPM. If this isn't a program you meant to run, stop it from the Rooms tab."})
    eff = flow_efficiency(samples)
    if len(eff) >= 2:
        lo, hi = eff[0], eff[-1]
        findings.append({"level": "good", "title": f"Lower flow moves water {lo['gal_per_kwh'] / hi['gal_per_kwh']:.1f}× cheaper",
                         "text": f"Measured: {lo['gpm']:.0f} GPM uses {lo['watts']} W ({lo['gal_per_kwh']:,} gal/kWh); "
                                 f"{hi['gpm']:.0f} GPM uses {hi['watts']} W ({hi['gal_per_kwh']:,} gal/kWh). "
                                 "Running longer at a lower flow inside the free window filters the same water for less delivery cost."})
    total_paid = sum(d["paid_kwh"] for d in energy)
    total_free = sum(d["free_kwh"] for d in energy)
    ndays = max(len(energy), 1)
    if health["ratio"] and health["ratio"] > 1 and energy:
        extra_kwh = (total_paid + total_free) / ndays * (1 - 1 / health["ratio"])
        if extra_kwh >= 0.05:
            findings.append({"level": "warning" if health["ratio"] >= WATCH else "good",
                             "title": f"Restriction costs about {extra_kwh * 30:.0f} kWh a month",
                             "text": f"The pump is drawing {(health['ratio'] - 1):.0%} more than with a clean filter."
                                     + (f" About ${extra_kwh * 30 * (paid_rate if total_paid > total_free else free_rate) / 100:.2f} a month." if priced else "")})
    cost = None
    if priced and energy:
        cost = {"paid": round(total_paid * paid_rate / 100, 2), "free": round(total_free * free_rate / 100, 2),
                "per_month": round((total_paid * paid_rate + total_free * free_rate) / 100 / ndays * 30, 2)}
    return {"live": live, "health": health, "energy": energy, "programs": sched, "efficiency": eff,
            "maintenance": effects or [{**m, "label": ACTIONS.get(m["action"], m["action"]), "effect": None} for m in maint[-10:]],
            "findings": findings, "window": {"start": window[0], "end": window[1]}, "cost": cost, "priced": priced,
            "samples": len(samples), "first_sample": samples[0]["t"] if samples else None,
            "actions": [{"action": k, "label": v} for k, v in ACTIONS.items()],
            "kwh": {"paid": round(total_paid, 1), "free": round(total_free, 1), "days": len(energy)}}


def _clock(minutes: int) -> str:
    h, m = divmod(minutes % 1440, 60)
    return f"{(h % 12) or 12}{f':{m:02d}' if m else ''} {'AM' if h < 12 else 'PM'}"
