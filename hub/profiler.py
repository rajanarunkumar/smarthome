"""Where the hub spends CPU: a sampling profiler for Admin → What's using CPU?

For a few seconds it looks at every thread's stack many times a second. A sample counts as busy unless the
thread is waiting (select/poll/kqueue, a lock, a sleep, a socket read). Busy samples are grouped by the hub code
involved (or, for library threads, the library), so the answer reads like "kasa_adapter.refresh: 40% of busy time".
Plain Python only, no extra packages, and it runs in a worker thread so the hub keeps answering meanwhile.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

HUB_DIR = str(Path(__file__).resolve().parent)
IDLE_FUNCS = {"select", "poll", "kqueue", "control", "epoll", "wait", "acquire", "sleep", "_wait_for_tstate_lock",
              "recv", "recv_into", "recvfrom", "accept", "readinto", "read", "readline", "_worker", "get", "join",
              "wait_for", "_recv", "communicate"}


def _where(frame) -> tuple[str, str]:
    """(hub code, library) for one stack: the innermost hub frame and the innermost library frame."""
    hub = lib = None
    f = frame
    while f is not None:
        path = f.f_code.co_filename
        if hub is None and path.startswith(HUB_DIR) and not path.endswith("profiler.py"):
            hub = f"{Path(path).stem}.{f.f_code.co_name}:{f.f_lineno}"
        if lib is None and "site-packages" in path:
            pkg = path.split("site-packages" + os.sep, 1)[1].split(os.sep, 1)[0]
            lib = f"{pkg} ({f.f_code.co_name})"
        f = f.f_back
    return hub or "", lib or ""


def _plain(frame) -> str:
    """file:function of the innermost named function (skipping <genexpr>, <listcomp> and the like)."""
    f = frame
    while f is not None and f.f_code.co_name.startswith("<"):
        f = f.f_back
    f = f or frame
    return f"{Path(f.f_code.co_filename).name}:{f.f_code.co_name}"


def _idle(frame) -> bool:
    return frame.f_code.co_name in IDLE_FUNCS


def sample(seconds: float = 15.0, interval: float = 0.01) -> dict:
    me = threading.get_ident()
    names = {t.ident: t.name for t in threading.enumerate()}
    total: Counter = Counter()
    busy: Counter = Counter()
    places: dict[int, Counter] = defaultdict(Counter)
    cpu0, t0 = sum(os.times()[:2]), time.monotonic()
    deadline = t0 + seconds
    while time.monotonic() < deadline:
        for ident, frame in sys._current_frames().items():
            if ident == me:
                continue
            total[ident] += 1
            if _idle(frame):
                continue
            busy[ident] += 1
            hub, lib = _where(frame)
            places[ident][" · ".join(x for x in (hub, lib) if x) or _plain(frame)] += 1
        time.sleep(interval)
    wall = time.monotonic() - t0
    cpu = sum(os.times()[:2]) - cpu0
    names.update({t.ident: t.name for t in threading.enumerate()})
    threads = []
    for ident, n in total.most_common():
        b = busy[ident]
        if not b:
            continue
        threads.append({"name": "event loop (main)" if ident == threading.main_thread().ident else names.get(ident, str(ident)),
                        "busy_pct": round(100 * b / n, 1),
                        "top": [{"where": w, "pct": round(100 * c / b, 1)} for w, c in places[ident].most_common(8)]})
    threads.sort(key=lambda t: -t["busy_pct"])
    return {"seconds": round(wall, 1), "cpu_pct": round(100 * cpu / wall, 1), "threads_seen": len(total),
            "threads": threads[:12]}
