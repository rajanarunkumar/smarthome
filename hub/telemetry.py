"""In-memory telemetry for the Admin tab: requests, brands, background jobs, event loop, process, logs.

Nothing is written to disk; it resets when the hub restarts. Standard library only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .cache import percentile

SLOW_MS = 1000
SAMPLE_SECONDS = 30
SERIES_POINTS = 720  # 6 hours at 30 s


def _r(x, n=1):
    return None if x is None else round(x, n)


@dataclass
class OpStats:
    calls: int = 0
    errors: int = 0
    timeouts: int = 0
    last_ms: float | None = None
    last_ok: float | None = None      # wall-clock time
    last_error: str | None = None
    last_error_at: float | None = None
    ms: deque = field(default_factory=lambda: deque(maxlen=200))

    def record(self, ms: float, error: str | None = None, timeout: bool = False) -> None:
        self.calls += 1
        self.last_ms = ms
        self.ms.append(ms)
        if error is None:
            self.last_ok = time.time()
        else:
            self.errors += 1
            self.timeouts += int(timeout)
            self.last_error, self.last_error_at = error, time.time()

    def snapshot(self) -> dict:
        return {"calls": self.calls, "errors": self.errors, "timeouts": self.timeouts,
                "last_ms": _r(self.last_ms), "p50_ms": _r(percentile(self.ms, 50)), "p95_ms": _r(percentile(self.ms, 95)),
                "last_ok": self.last_ok, "last_error": self.last_error, "last_error_at": self.last_error_at}


@dataclass
class RouteStats:
    count: int = 0
    err4: int = 0
    err5: int = 0
    max_ms: float = 0.0
    ms: deque = field(default_factory=lambda: deque(maxlen=500))


class LogRing(logging.Handler):
    """Keeps the last WARNING-and-above records in memory."""

    def __init__(self, size: int = 200) -> None:
        super().__init__(level=logging.WARNING)
        self.records: deque = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        self.records.append({"t": record.created, "level": record.levelname, "logger": record.name, "msg": msg[:500]})


class Telemetry:
    def __init__(self, data_dir: Path | None = None, clock=time.monotonic) -> None:
        self.clock = clock
        self.started = time.time()
        self.data_dir = data_dir
        self.routes: dict[str, RouteStats] = {}
        self.slow: deque = deque(maxlen=50)
        self.ops: dict[tuple[str, str], OpStats] = {}
        self.jobs: dict[str, dict] = {}
        self.activity: deque = deque(maxlen=50)
        self.loop_lag: deque = deque(maxlen=600)  # 5 min at 0.5 s
        self.series: deque = deque(maxlen=SERIES_POINTS)
        self.logs = LogRing()
        self._req_times: deque = deque(maxlen=20000)  # wall-clock of recent requests, for req/min
        self._cpu_prev = (time.monotonic(), sum(os.times()[:2]))
        self._rss_cache = (0.0, None)
        self._commit = _git_commit()
        self._tasks: list[asyncio.Task] = []

    # ----- recording -----
    def record_request(self, route: str, status: int, ms: float, method: str = "GET") -> None:
        key = f"{method} {route}"
        st = self.routes.setdefault(key, RouteStats())
        st.count += 1
        st.ms.append(ms)
        st.max_ms = max(st.max_ms, ms)
        if 400 <= status < 500:
            st.err4 += 1
        elif status >= 500:
            st.err5 += 1
        self._req_times.append(time.time())
        if ms >= SLOW_MS:
            self.slow.append({"t": time.time(), "route": key, "status": status, "ms": round(ms)})

    def op(self, brand: str, name: str) -> OpStats:
        return self.ops.setdefault((brand, name), OpStats())

    def job(self, name: str, started: float, ms: float, ok: bool, detail: str | None = None,
            next_in: float | None = None) -> None:
        j = self.jobs.setdefault(name, {"runs": 0, "failures": 0})
        j.update(runs=j["runs"] + 1, failures=j["failures"] + int(not ok), last_start=started,
                 last_ms=round(ms), ok=ok, detail=detail,
                 next_run=(time.time() + next_in) if next_in else None)

    def add_activity(self, who: str | None, what: str, ms: float, result: str, ok: bool = True) -> None:
        self.activity.appendleft({"t": time.time(), "who": who, "what": what, "ms": round(ms), "result": result, "ok": ok})

    # ----- background samplers -----
    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._lag_loop()), asyncio.create_task(self._sample_loop())]

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    async def _lag_loop(self) -> None:
        while True:
            t = time.monotonic()
            await asyncio.sleep(0.5)
            self.loop_lag.append(max(0.0, (time.monotonic() - t - 0.5) * 1000))

    async def _sample_loop(self) -> None:
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - telemetry must never take the hub down
                pass

    def sample(self) -> dict:
        p = self.process()
        dev = self.routes.get("GET /api/devices")
        point = {"t": time.time(), "rss_mb": p["rss_mb"], "cpu_pct": p["cpu_pct"],
                 "lag_p95_ms": _r(percentile(list(self.loop_lag)[-60:], 95)),
                 "req_per_min": self.requests_per_minute(),
                 "devices_p95_ms": _r(percentile(list(dev.ms)[-50:], 95)) if dev else None}
        self.series.append(point)
        return point

    def requests_per_minute(self) -> int:
        cutoff = time.time() - 60
        return sum(1 for t in self._req_times if t >= cutoff)

    # ----- process and host -----
    def _rss_mb(self) -> float | None:
        at, val = self._rss_cache
        if time.monotonic() - at < 10 and val is not None:
            return val
        try:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, timeout=2)
            val = round(int(out.stdout.strip()) / 1024, 1)
        except Exception:  # noqa: BLE001
            val = None
        self._rss_cache = (time.monotonic(), val)
        return val

    def _cpu_pct(self) -> float | None:
        now, cpu = time.monotonic(), sum(os.times()[:2])
        prev_t, prev_cpu = self._cpu_prev
        if now - prev_t < 1.0:  # too short to measure: keep the last reading
            return getattr(self, "_cpu_last", None)
        self._cpu_prev = (now, cpu)
        self._cpu_last = round((cpu - prev_cpu) / (now - prev_t) * 100, 1)
        return self._cpu_last

    def process(self) -> dict:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mb = peak / 1024 / 1024 if sys.platform == "darwin" else peak / 1024
        try:
            fds = len(os.listdir("/dev/fd"))
        except OSError:
            fds = None
        try:
            tasks = len(asyncio.all_tasks())
        except RuntimeError:
            tasks = None
        return {"pid": os.getpid(), "python": sys.version.split()[0], "commit": self._commit,
                "started": self.started, "uptime_s": round(time.time() - self.started),
                "cpu_pct": self._cpu_pct(), "rss_mb": self._rss_mb(), "peak_rss_mb": round(peak_mb, 1),
                "threads": threading.active_count(), "open_fds": fds, "asyncio_tasks": tasks}

    def host(self) -> dict:
        out: dict = {"load_avg": [round(x, 2) for x in os.getloadavg()]}
        if self.data_dir:
            du = shutil.disk_usage(self.data_dir)
            out.update(disk_free_gb=round(du.free / 1e9, 1), disk_total_gb=round(du.total / 1e9, 1))
            sizes = {}
            for name in ("hub.db", "hub.db-wal", "logs/hub.log", "secrets.json"):
                f = self.data_dir / name
                if f.exists():
                    sizes[name] = f.stat().st_size
            photos = self.data_dir / "photos"
            if photos.is_dir():
                sizes["photos/"] = sum(f.stat().st_size for f in photos.rglob("*") if f.is_file())
            out["files"] = sizes
        return out

    # ----- reports -----
    def loop(self) -> dict:
        lag = list(self.loop_lag)
        return {"lag_last_ms": _r(lag[-1]) if lag else None, "lag_p95_ms": _r(percentile(lag, 95)),
                "lag_max_ms": _r(max(lag)) if lag else None}

    def routes_report(self) -> list[dict]:
        rows = [{"route": k, "count": s.count, "err4": s.err4, "err5": s.err5,
                 "p50_ms": _r(percentile(s.ms, 50)), "p95_ms": _r(percentile(s.ms, 95)),
                 "p99_ms": _r(percentile(s.ms, 99)), "max_ms": _r(s.max_ms)} for k, s in self.routes.items()]
        return sorted(rows, key=lambda r: -(r["p95_ms"] or 0))

    def ops_report(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for (brand, name), st in self.ops.items():
            out.setdefault(brand, {})[name] = st.snapshot()
        return out

    def log_report(self, level: str = "WARNING", limit: int = 200) -> list[dict]:
        floor = logging.getLevelName(level.upper()) if isinstance(logging.getLevelName(level.upper()), int) else 30
        rows = [r for r in self.logs.records if logging.getLevelName(r["level"]) >= floor]
        return list(reversed(rows))[:limit]


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
                             capture_output=True, text=True, timeout=2)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None
