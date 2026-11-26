"""Numbers in the unit a person would use: 45 min, 1 h 20 min; 6.8 W, 680 W, 1.5 kW; 350 Wh, 4.2 kWh.
The web app has the same rules in app.js (dur, power, energy)."""

from __future__ import annotations


def dur(minutes: float | None) -> str:
    if minutes is None:
        return "–"
    m = round(minutes)
    if minutes > 0 and m == 0:
        return "under 1 min"
    if m < 60:
        return f"{m} min"
    if m < 24 * 60:
        h, rest = divmod(m, 60)
        return f"{h} h {rest} min" if rest else f"{h} h"
    d, rest = divmod(round(minutes / 60), 24)
    return f"{d} d {rest} h" if rest else f"{d} d"


def power(watts: float | None) -> str:
    if watts is None:
        return "–"
    if abs(watts) < 10:
        return f"{watts:.1f} W"
    if abs(watts) < 1000:
        return f"{watts:,.0f} W"
    return f"{watts / 1000:,.1f} kW"


def energy(kwh: float | None) -> str:
    if kwh is None:
        return "–"
    if abs(kwh) < 1:
        return f"{kwh * 1000:,.0f} Wh"
    if abs(kwh) < 10:
        return f"{kwh:.1f} kWh"
    return f"{kwh:,.0f} kWh"
