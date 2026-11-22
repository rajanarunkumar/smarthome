"""Time-of-use view of the whole house, for TXU's free-nights plan.

Energy is free from free_start to free_end (8 PM to 4 AM by default), but Oncor's
delivery charge is still paid on every kWh, day or night. So there are two ways
to save, and both are measured here from Smart Meter Texas's 15-minute data:

1. Move use out of paid hours into the free window (saves energy + delivery).
2. Use fewer kWh at night too (saves delivery only).

Prices come from a bill (Settings → Electricity plan); without them only kWh are shown.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .pool import in_free


def tou_report(intervals: dict[str, float], tariff: dict) -> dict | None:
    if not intervals:
        return None
    window = (int(tariff.get("free_start", 20)), int(tariff.get("free_end", 4)))
    energy_c = tariff.get("energy_cents")
    delivery_c = tariff.get("delivery_cents")
    night_c = tariff.get("night_delivery_cents", delivery_c)
    fees = 1 + (tariff.get("fees_pct") or 0) / 100
    priced = energy_c is not None and delivery_c is not None
    by_hour: dict[int, float] = defaultdict(float)
    daily: dict[str, dict] = defaultdict(lambda: {"free": 0.0, "paid": 0.0})
    for slot, kwh in intervals.items():
        t = datetime.fromisoformat(slot)
        by_hour[t.hour] += kwh
        daily[slot[:10]]["free" if in_free(t, window) else "paid"] += kwh
    days = len(daily)
    free = sum(d["free"] for d in daily.values())
    paid = sum(d["paid"] for d in daily.values())
    hours = [{"hour": h, "kwh": round(by_hour[h] / days, 2), "free": in_free(datetime(2000, 1, 1, h), window)} for h in range(24)]
    paid_hours = [h for h in hours if not h["free"]]
    # The paid hours just before the window opens: the easiest use to delay.
    lead = [h for h in paid_hours if (window[0] - h["hour"]) % 24 in (1, 2, 3)]
    floor = sorted(h["kwh"] for h in paid_hours)[len(paid_hours) // 4] if paid_hours else 0  # quiet paid hour
    shiftable = sum(max(h["kwh"] - floor, 0) for h in lead)
    recs = []
    recs.append({"level": "good" if paid <= free else "warning",
                 "title": f"{paid / (free + paid):.0%} of your electricity is in paid hours",
                 "text": f"{paid / days:.1f} kWh a day paid, {free / days:.1f} kWh a day in the free window "
                         f"({_h(window[0])}–{_h(window[1])}), over {days} days."})
    if shiftable >= 0.5:
        hrs = ", ".join(f"{_h(h['hour'])} {h['kwh']:.1f} kWh" for h in lead)
        recs.append({"level": "warning", "title": f"About {shiftable:.1f} kWh a day could wait until {_h(window[0])}",
                     "text": f"Use climbs in the last paid hours ({hrs}) above the quiet-hour level of {floor:.1f} kWh. "
                             "Dishwasher, laundry, EV charging and the pool pump can start at 8 PM instead; "
                             "pre-cooling the house after 8 PM and setting the thermostat 2–3 °F higher from 5 PM also helps."
                             + (f" Worth about ${shiftable * (energy_c + delivery_c - night_c) * fees / 100 * 30:.0f} a month." if priced else "")})
    if priced:
        recs.append({"level": "good", "title": f"Night use still costs about ${free / days * night_c * fees / 100 * 30:.0f} a month in delivery",
                     "text": f"{free / days:.1f} kWh a night × {night_c:.2f}¢ delivery. Free energy isn't free delivery: "
                             "lower pool pump flow and a slightly warmer AC setting at night cut this too."})
    cost = None
    if priced:
        cost = {"energy": round(paid * energy_c / 100, 2), "delivery": round((paid * delivery_c + free * night_c) / 100, 2),
                "fixed": round((tariff.get("monthly_fee") or 0) * days / 30, 2)}
        cost["fees"] = round(sum(cost.values()) * (fees - 1), 2)
        cost["total"] = round(sum(cost.values()), 2)
        cost["per_month"] = round(cost["total"] / days * 30, 2)
    return {"window": {"start": window[0], "end": window[1]}, "days": days,
            "free_kwh": round(free, 1), "paid_kwh": round(paid, 1), "hours": hours,
            "daily": [{"day": d, "free_kwh": round(v["free"], 1), "paid_kwh": round(v["paid"], 1)} for d, v in sorted(daily.items())],
            "priced": priced, "cost": cost, "tariff": tariff, "recommendations": recs}


def _h(hour: int) -> str:
    return f"{(hour % 12) or 12} {'AM' if hour % 24 < 12 else 'PM'}"
