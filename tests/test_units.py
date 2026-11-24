"""Units: durations in minutes and hours (never 0.3 h), power in W or kW, energy in Wh or kWh; the web app agrees."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hub.units import dur, energy, power

CASES_DUR = [(None, "–"), (0, "0 min"), (0.2, "under 1 min"), (20, "20 min"), (59.6, "1 h"), (60, "1 h"), (80, "1 h 20 min"),
             (600, "10 h"), (1439, "23 h 59 min"), (1500, "1 d 1 h"), (2880, "2 d")]
CASES_POWER = [(6.8, "6.8 W"), (680, "680 W"), (999, "999 W"), (1500, "1.5 kW")]
CASES_ENERGY = [(0.35, "350 Wh"), (4.2, "4.2 kWh"), (1771, "1,771 kWh")]


def test_python_units():
    assert [dur(m) for m, _ in CASES_DUR] == [s for _, s in CASES_DUR]
    assert [power(w) for w, _ in CASES_POWER] == [s for _, s in CASES_POWER]
    assert [energy(k) for k, _ in CASES_ENERGY] == [s for _, s in CASES_ENERGY]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_web_app_formats_the_same_way():
    src = Path("hub/web/app.js").read_text()
    funcs = "\n".join(re.search(rf"function {name}\(.*?\n}}\n", src, re.S).group(0)
                      for name in ("fmtDuration", "fmtPower", "fmtEnergy"))
    script = funcs + f"""
console.log(JSON.stringify([{json.dumps([m for m, _ in CASES_DUR])}.map(fmtDuration),
  {json.dumps([w for w, _ in CASES_POWER])}.map(fmtPower), {json.dumps([k for k, _ in CASES_ENERGY])}.map(fmtEnergy)]));"""
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out == [[s for _, s in CASES_DUR], [s for _, s in CASES_POWER], [s for _, s in CASES_ENERGY]]
