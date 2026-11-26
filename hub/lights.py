"""Power of smart bulbs from their model, without measuring: a baseline for the electricity breakdown.

Smart bulbs don't report power, but each reports its model, and bulbs of one model draw the same. The table in
hub/data/light_power.json comes from the Powercalc profile library (MIT License, Copyright (c) 2021 Bram Gerritsen
and contributors, https://github.com/bramstroker/homeassistant-powercalc), where each model was measured at every
brightness (colour bulbs averaged over their colours). Models it doesn't have get a typical figure, marked as such.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "light_power.json"
# Published ratings for common models missing from the measured library (watts at full brightness).
RATED = {"H6008": ("Govee A19 bulb", 9.0), "H6009": ("Govee A19 bulb", 9.0), "H6006": ("Govee A19 bulb", 9.0)}
TYPICAL_BULB_W, TYPICAL_STRIP_W, TYPICAL_STANDBY_W = 9.0, 20.0, 0.4


@lru_cache(maxsize=1)
def _table() -> dict:
    try:
        return json.loads(DATA.read_text())["models"]
    except (OSError, ValueError, KeyError):
        return {}


def _keys(model_id: str | None, model: str | None) -> list[str]:
    out = []
    for raw in (model_id, model):
        if raw:
            key = re.sub(r"\(.*?\)", "", str(raw)).strip().upper()   # "KL135(US)" -> "KL135"
            out += [key, key.split()[0]] if " " in key else [key]
    return out


def profile(model_id: str | None, model: str | None, name: str = "") -> dict:
    """{"name", "curve" (W at 0..100% brightness), "standby", "source": measured | rated | typical}."""
    table = _table()
    for key in _keys(model_id, model):
        if key in table:
            return {**table[key], "source": "measured"}
        if key in RATED:
            label, watts = RATED[key]
            return {"name": label, "curve": [round(watts * max(i, 1) / 10, 2) for i in range(11)],
                    "standby": TYPICAL_STANDBY_W, "source": "rated"}
    text = f"{model or ''} {name}".lower()
    watts = TYPICAL_STRIP_W if "strip" in text else TYPICAL_BULB_W
    return {"name": "typical LED " + ("strip" if "strip" in text else "bulb"),
            "curve": [round(watts * max(i, 1) / 10, 2) for i in range(11)], "standby": TYPICAL_STANDBY_W, "source": "typical"}


def watts_at(prof: dict, brightness: int | None) -> float:
    """Power when on at a brightness (1-100; None means full), interpolated between the 10% steps."""
    curve = prof["curve"]
    b = 100 if brightness is None else max(1, min(100, brightness))
    pos = b / 10
    lo = int(pos)
    if lo >= 10:
        return curve[10]
    return round(curve[lo] + (curve[lo + 1] - curve[lo]) * (pos - lo), 2)
