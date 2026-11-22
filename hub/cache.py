"""A small in-memory loading cache for asyncio, in the spirit of Java's Caffeine.

- refresh_after: an entry older than this is returned at once and reloaded in the background.
- expire_after: an entry older than this makes the caller wait for a reload; if the reload fails or
  times out, the old value is still served (marked stale) rather than an error.
- Single flight: concurrent gets for the same key share one load.
- Size bound with least-recently-used eviction.
- A failed load backs off before the next background refresh, so a locked-out device isn't hammered.
"""

from __future__ import annotations

import asyncio
import math
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Hashable

log = logging.getLogger(__name__)
NEVER = float("-inf")


@dataclass
class Entry:
    value: Any
    written: float
    stale: bool = False
    error: str | None = None
    error_at: float = NEVER


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    loads: int = 0
    load_failures: int = 0
    refreshes: int = 0
    evictions: int = 0
    timeouts: int = 0
    stale_serves: int = 0
    load_ms: deque = field(default_factory=lambda: deque(maxlen=200))


def percentile(values, q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100) of a sequence of numbers; None if empty."""
    data = sorted(values)
    if not data:
        return None
    rank = max(1, min(len(data), math.ceil(q / 100 * len(data))))
    return data[rank - 1]


class LoadingCache:
    def __init__(self, name: str, loader: Callable[[Hashable], Awaitable[Any]], *, max_size: int = 128,
                 refresh_after: float = 10.0, expire_after: float = 120.0, timeout: float | None = None,
                 failure_backoff: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self.loader = loader
        self.max_size = max_size
        self.refresh_after = refresh_after
        self.expire_after = expire_after
        self.timeout = timeout
        self.failure_backoff = failure_backoff
        self.clock = clock
        self.stats = CacheStats()
        self._entries: OrderedDict[Hashable, Entry] = OrderedDict()
        self._loading: dict[Hashable, asyncio.Task] = {}
        self._load_started: dict[Hashable, float] = {}
        self._failed: dict[Hashable, tuple[float, BaseException]] = {}  # keys with no value whose last load failed

    # ----- reads -----
    def entry(self, key: Hashable = None) -> Entry | None:
        return self._entries.get(key)

    def get_if_present(self, key: Hashable = None) -> Any:
        e = self._entries.get(key)
        return None if e is None else e.value

    def age(self, key: Hashable = None) -> float | None:
        e = self._entries.get(key)
        return None if e is None else self.clock() - e.written

    async def get(self, key: Hashable = None, *, timeout: float | None = None) -> Any:
        timeout = self.timeout if timeout is None else timeout
        e = self._entries.get(key)
        now = self.clock()
        if e is not None:
            self._entries.move_to_end(key)
            age = now - e.written
            if age < self.expire_after:
                self.stats.hits += 1
                if age >= self.refresh_after and now - e.error_at >= self.failure_backoff:
                    self._start_load(key, refresh=True)
                if e.stale:
                    self.stats.stale_serves += 1
                return e.value
            # Expired: wait for a reload, but fall back to the old value.
            self.stats.misses += 1
            try:
                return await self._wait(key, timeout)
            except Exception as exc:  # noqa: BLE001 - serve the last good value instead
                e.stale = True
                e.error = e.error or _describe(exc)
                self.stats.stale_serves += 1
                return e.value
        self.stats.misses += 1
        # Nothing cached yet. Don't make every caller wait again for a load that is already running
        # past the time limit, or retry a load that just failed: answer at once.
        started = self._load_started.get(key)
        if timeout is not None and key in self._loading and started is not None and now - started >= timeout:
            self.stats.timeouts += 1
            raise asyncio.TimeoutError()
        failed = self._failed.get(key)
        if failed and now - failed[0] < self.failure_backoff:
            raise failed[1]
        return await self._wait(key, timeout)

    async def refresh(self, key: Hashable = None, *, timeout: float | None = None) -> Any:
        """Reload now and wait (bypasses freshness). Raises if the load fails."""
        return await self._wait(key, self.timeout if timeout is None else timeout, force=True)

    def warm(self, keys) -> int:
        """Start background loads for keys that are missing or due for refresh. Returns how many started."""
        started = 0
        now = self.clock()
        for key in keys:
            e = self._entries.get(key)
            due = e is None or (now - e.written >= self.refresh_after and now - e.error_at >= self.failure_backoff)
            if due and key not in self._loading:
                self._start_load(key, refresh=e is not None)
                started += 1
        return started

    # ----- writes -----
    def put(self, key: Hashable, value: Any) -> None:
        self._entries[key] = Entry(value, self.clock())
        self._entries.move_to_end(key)
        self._evict()

    def touch_for_refresh(self, key: Hashable = None) -> None:
        """Keep the value but make it due for a background refresh on the next get."""
        e = self._entries.get(key)
        if e is not None:
            e.written = min(e.written, self.clock() - self.refresh_after)
            e.error_at = NEVER

    def invalidate(self, key: Hashable = None) -> None:
        self._entries.pop(key, None)
        self._failed.pop(key, None)

    def invalidate_all(self) -> None:
        self._entries.clear()
        self._failed.clear()

    def keys(self) -> list:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    # ----- loading -----
    def _start_load(self, key: Hashable, refresh: bool = False) -> asyncio.Task:
        task = self._loading.get(key)
        if task is None:
            if refresh:
                self.stats.refreshes += 1
            task = asyncio.create_task(self._load(key))
            self._loading[key] = task
            self._load_started[key] = self.clock()
        return task

    async def _wait(self, key: Hashable, timeout: float | None, force: bool = False) -> Any:
        task = self._start_load(key, refresh=force and key in self._entries)
        try:
            if timeout is None:
                return await asyncio.shield(task)
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            self.stats.timeouts += 1
            raise

    async def _load(self, key: Hashable) -> Any:
        start = self.clock()
        self.stats.loads += 1
        try:
            value = await self.loader(key)
        except Exception as exc:
            self.stats.load_failures += 1
            e = self._entries.get(key)
            if e is not None:
                e.stale, e.error, e.error_at = True, _describe(exc), self.clock()
            else:
                self._failed[key] = (self.clock(), exc)
            raise
        finally:
            self.stats.load_ms.append((self.clock() - start) * 1000)
            self._loading.pop(key, None)
            self._load_started.pop(key, None)
        self._failed.pop(key, None)
        self.put(key, value)
        return value

    def _evict(self) -> None:
        while len(self._entries) > self.max_size:
            self._entries.popitem(last=False)
            self.stats.evictions += 1

    # ----- reporting -----
    def snapshot(self) -> dict:
        s = self.stats
        lookups = s.hits + s.misses
        now = self.clock()
        ages = [now - e.written for e in self._entries.values()]
        return {
            "name": self.name, "size": len(self._entries), "max_size": self.max_size,
            "refresh_after_s": self.refresh_after, "expire_after_s": self.expire_after,
            "hits": s.hits, "misses": s.misses, "hit_rate": round(s.hits / lookups, 3) if lookups else None,
            "loads": s.loads, "load_failures": s.load_failures, "refreshes": s.refreshes,
            "evictions": s.evictions, "timeouts": s.timeouts, "stale_serves": s.stale_serves,
            "loading": len(self._loading),
            "load_ms_avg": round(sum(s.load_ms) / len(s.load_ms), 1) if s.load_ms else None,
            "load_ms_p95": _round(percentile(s.load_ms, 95)),
            "oldest_s": round(max(ages), 1) if ages else None,
            "entries": [{"key": str(k), "age_s": round(now - e.written, 1), "stale": e.stale, "error": e.error}
                        for k, e in self._entries.items()],
        }


def _round(x):
    return None if x is None else round(x, 1)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    return str(getattr(exc, "message", None) or exc) or type(exc).__name__
