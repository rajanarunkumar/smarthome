"""Build hub/data/light_power.json from Powercalc's measured profiles (MIT, Bram Gerritsen and contributors)."""
# Usage: python scripts/build_light_power.py <homeassistant-powercalc>/profile_library hub/data/light_power.json
import csv, gzip, json, sys
from pathlib import Path
from collections import defaultdict

lib = Path(sys.argv[1]); out = Path(sys.argv[2])
POINTS = [round(255 * p / 10) or 1 for p in range(11)]   # brightness 0%..100% as 1..255

def lut_curve(folder):
    files = [f for f in ("brightness.csv.gz", "color_temp.csv.gz", "hs.csv.gz") if (folder / f).exists()]
    if not files:
        return None
    by_bri = defaultdict(list)
    with gzip.open(folder / files[0], "rt") as fh:   # brightness-only first, then white, then colour
        for row in csv.DictReader(fh):
            try:
                by_bri[int(row["bri"])].append(float(row["watt"]))
            except (KeyError, ValueError):
                pass
    if not by_bri:
        return None
    levels = sorted(by_bri)
    avg = {b: sum(v) / len(v) for b, v in by_bri.items()}
    curve = []
    for p in POINTS:
        nearest = min(levels, key=lambda b: abs(b - p))
        curve.append(round(avg[nearest], 2))
    return curve

table, skipped = {}, 0
for maker in ("signify", "govee", "tp-link"):
    for folder in sorted((lib / maker).iterdir()):
        mj = folder / "model.json"
        if not mj.exists():
            continue
        m = json.loads(mj.read_text())
        if m.get("device_type", "light") != "light":
            continue
        strategy = m.get("calculation_strategy", "lut")
        curve = None
        if strategy == "lut":
            curve = lut_curve(folder)
        elif strategy == "linear":
            lin = m.get("linear_config") or m.get("linear") or {}
            lo, hi = lin.get("min_power"), lin.get("max_power")
            if hi is not None:
                lo = lo or 0
                curve = [round(lo + (hi - lo) * i / 10, 2) for i in range(11)]
        elif strategy == "fixed":
            power = (m.get("fixed_config") or m.get("fixed") or {}).get("power")
            if isinstance(power, (int, float)):
                curve = [round(power, 2)] * 11
        if not curve:
            skipped += 1
            continue
        entry = {"name": m.get("name", folder.name), "curve": curve, "standby": round(m.get("standby_power") or 0, 2)}
        for key in [folder.name, *m.get("aliases", [])]:
            table.setdefault(str(key).upper(), entry)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"_source": "Powercalc profile library (MIT License, Copyright (c) 2021 Bram Gerritsen and contributors), "
                           "https://github.com/bramstroker/homeassistant-powercalc. Watts at brightness 0%..100% in 10% steps; "
                           "colour bulbs averaged over their measured colours.", "models": table}, separators=(",", ":"), sort_keys=True))
print(len(table), "keys", skipped, "skipped", out.stat().st_size, "bytes")
