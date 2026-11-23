"""Each TXU bill against the meter: where the dollars went, by time of day.

For a bill period, the meter's 15-minute kWh (Smart Meter Texas) are added up in four parts of the day:

    Night      8 PM–4 AM   free energy; delivery and fees still paid
    Morning    4 AM–noon   paid
    Afternoon  noon–4 PM   paid
    Evening    4 PM–8 PM   paid

and priced with that bill's own numbers: its energy charge (after the free-nights credit) spread over the
paid kWh, its delivery charge split into night and day (the old plan discounted night delivery), the base
charge, and gross-receipts and sales tax in proportion. If the meter covers the whole period, the rebuilt
total matches the bill; the difference is the evidence that the split is right.

Two more figures per bill:
- Seasonal extra: kWh per day above the same part of the day in your mildest month. Mostly heating and
  cooling (the pool pump also runs longer in summer). Replaced by measured thermostat run time once a
  thermostat is connected.
- Against the same period a year earlier: last year's kWh in each part of the day, priced at this bill's
  prices, minus what this bill cost. Rate changes drop out, so what's left is use (and the weather).
"""

from __future__ import annotations

from datetime import date, timedelta

from .bills import night_delivery_cents

PARTS = [("night", "Night (free)", 20, 4), ("morning", "Morning", 4, 12), ("afternoon", "Afternoon", 12, 16),
         ("evening", "Evening", 16, 20)]
FULL_DAY = 92            # of 96 slots: a day the meter covered


def part_of(hour: int) -> str:
    for key, _, start, end in PARTS:
        if (start <= hour < end) if start < end else (hour >= start or hour < end):
            return key
    return "night"


def meter_parts(intervals: dict[str, float], start: str, end: str) -> dict:
    """kWh per part of the day for start <= day < end, and how many days the meter fully covered."""
    kwh = {k: 0.0 for k, *_ in PARTS}
    per_day: dict[str, int] = {}
    for slot, v in intervals.items():
        day = slot[:10]
        if start <= day < end:
            kwh[part_of(int(slot[11:13]))] += v
            per_day[day] = per_day.get(day, 0) + 1
    return {"kwh": kwh, "days_full": sum(1 for n in per_day.values() if n >= FULL_DAY), "days_seen": len(per_day)}


def prices(bill: dict) -> dict:
    """This bill's cents per kWh, before fees: energy in paid hours, delivery by night and day."""
    paid = bill["paid_kwh"] or 1
    energy_net = bill["energy"] - bill["free_credit"]
    night_dc = night_delivery_cents(bill)
    delivery_net = bill["tdu"] - bill["tdu_credit"]
    day_dc = (delivery_net * 100 - night_dc * bill["free_kwh"]) / paid
    fees = (bill["grt"] + bill["tax"]) / max(bill["total"] - bill["grt"] - bill["tax"], 1)
    return {"energy": energy_net * 100 / paid, "night_delivery": night_dc, "day_delivery": day_dc, "fees": fees}


def price_parts(kwh: dict[str, float], p: dict) -> list[dict]:
    rows = []
    for key, label, start, end in PARTS:
        k = kwh.get(key, 0.0)
        energy = 0.0 if key == "night" else k * p["energy"] / 100
        delivery = k * (p["night_delivery"] if key == "night" else p["day_delivery"]) / 100
        fees = (energy + delivery) * p["fees"]
        rows.append({"part": key, "label": label, "hours": f"{_h(start)}–{_h(end)}", "kwh": round(k, 1),
                     "energy": round(energy, 2), "delivery": round(delivery, 2), "fees": round(fees, 2),
                     "total": round(energy + delivery + fees, 2),
                     "cents_per_kwh": round((energy + delivery + fees) / k * 100, 1) if k else None})
    return rows


def reconcile_bill(bill: dict, intervals: dict[str, float]) -> dict:
    m = meter_parts(intervals, bill["period_start"], bill["period_end"])
    p = prices(bill)
    parts = price_parts(m["kwh"], p)
    meter_kwh = sum(m["kwh"].values())
    base = bill["base"] * (1 + p["fees"])
    rebuilt = sum(r["total"] for r in parts) + base + (bill.get("misc") or 0)
    complete = m["days_full"] >= bill["days"] - 1
    return {"period_start": bill["period_start"], "period_end": bill["period_end"], "days": bill["days"], "plan": bill["plan"],
            "billed_total": bill["total"], "billed_kwh": bill["kwh"], "billed_free_kwh": bill["free_kwh"],
            "meter_kwh": round(meter_kwh, 1), "meter_free_kwh": round(m["kwh"]["night"], 1),
            "coverage": round(m["days_full"] / bill["days"], 2), "complete": complete,
            "base": round(base, 2), "misc": bill.get("misc") or 0, "rebuilt_total": round(rebuilt, 2),
            "difference": round(rebuilt - bill["total"], 2) if complete else None,
            "parts": parts, "prices": {k: round(v, 3) for k, v in p.items()},
            "delivery_total": round(sum(r["delivery"] for r in parts), 2),
            "energy_total": round(sum(r["energy"] for r in parts), 2),
            "fees_total": round(sum(r["fees"] for r in parts) + base - bill["base"], 2)}


def seasonal_extra(recs: list[dict]) -> None:
    """kWh a day above the mildest complete month, per part of the day, priced at each bill's own prices."""
    full = [r for r in recs if r["complete"]]
    if len(full) < 3:
        return
    floor = {key: min(next(x["kwh"] for x in r["parts"] if x["part"] == key) / r["days"] for r in full) for key, *_ in PARTS}
    for r in recs:
        if not r["complete"]:
            r["seasonal"] = None
            continue
        extra_kwh = extra_usd = 0.0
        for x in r["parts"]:
            extra = max(x["kwh"] - floor[x["part"]] * r["days"], 0.0)
            x["seasonal_kwh"] = round(extra, 1)
            x["seasonal_usd"] = round(extra * (x["cents_per_kwh"] or 0) / 100, 2)
            extra_kwh += extra
            extra_usd += x["seasonal_usd"]
        r["seasonal"] = {"kwh": round(extra_kwh, 1), "usd": round(extra_usd, 2)}


def year_over_year(recs: list[dict], bills: list[dict]) -> None:
    """Last year's use, per part of the day, at this bill's prices, minus this bill's cost (both per day × this period)."""
    for r in recs:
        r["vs_last_year"] = None
        if not r["complete"]:
            continue
        end = date.fromisoformat(r["period_end"])
        prev = min((x for x in recs if x["complete"] and 350 <= (end - date.fromisoformat(x["period_end"])).days <= 380),
                   key=lambda x: abs((end - date.fromisoformat(x["period_end"])).days - 365), default=None)
        if not prev:
            continue
        bill = next(b for b in bills if b["period_end"] == r["period_end"])
        p = prices(bill)
        scale = r["days"] / prev["days"]
        then = price_parts({x["part"]: x["kwh"] * scale for x in prev["parts"]}, p)
        now_cost = sum(x["total"] for x in r["parts"])
        then_cost = sum(x["total"] for x in then)
        r["vs_last_year"] = {"period_end": prev["period_end"], "saved": round(then_cost - now_cost, 2),
                             "kwh_change": round(r["meter_kwh"] - prev["meter_kwh"] * scale, 1),
                             "parts": {x["part"]: round(t["total"] - x["total"], 2) for x, t in zip(r["parts"], then)}}


def reconcile_report(bills: list[dict], intervals: dict[str, float], target: float = 50.0) -> dict:
    recs = [reconcile_bill(b, intervals) for b in bills]
    seasonal_extra(recs)
    year_over_year(recs, bills)
    tracked = [r["vs_last_year"]["saved"] for r in recs if r.get("vs_last_year")]
    first_needed = min((b["period_start"] for b in bills), default=None)
    return {"bills": recs, "target": target, "parts": [{"part": k, "label": l, "hours": f"{_h(s)}–{_h(e)}"} for k, l, s, e in PARTS],
            "savings": {"months": len(tracked), "total": round(sum(tracked), 2),
                        "average": round(sum(tracked) / len(tracked), 2) if tracked else None},
            "meter_from": first_needed,
            "missing_days": sum(r["days"] - min(r["days"], round(r["coverage"] * r["days"])) for r in recs)}


def days_needed(bills: list[dict], have: set[str], today: date | None = None) -> list[str]:
    """Bill-period days the meter history doesn't have yet (Smart Meter Texas keeps about two years)."""
    if not bills:
        return []
    today = today or date.today()
    start = date.fromisoformat(min(b["period_start"] for b in bills))
    out = []
    d = start
    while d < today:
        if d.isoformat() not in have and (today - d).days <= 700:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _h(hour: int) -> str:
    return "noon" if hour == 12 else f"{(hour % 12) or 12} {'AM' if hour % 24 < 12 else 'PM'}"

