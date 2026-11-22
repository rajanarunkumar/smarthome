# Plan: snappier UI, faster hub, Admin telemetry (2026-11-21)

The user asked for three things: fix tabs and toggles that feel stuck or sticky, make the app faster, and add an **Admin** area with system telemetry and observability.
Nothing below is implemented yet. Pick up from step 1.

## What was measured (Mac, 23:30 CST)

| Endpoint | Time | Size |
|---|---|---|
| `GET /api/devices` | **19.0 s** (every call) | 34 KB |
| `GET /api/scenes` | 0.67 s | 25 KB |
| `GET /api/schedules` | 0.06 s | 7 KB |
| `GET /api/status`, `/api/insights`, `/api/storage`, `/api/auth/me` | < 0.01 s | small |

- Static files (`app.js` 91 KB, `insights.js` 30 KB, `style.css` 20 KB) are served with an ETag, but with no gzip and no `Cache-Control`.

## Why it feels stuck or sticky

1. **`/api/devices` is slow.** `Hub.devices()` waits on `gather()` of every adapter's live `devices()`, so the slowest brand (19 s) blocks everything. The Rooms tab, the Settings tab, every room *All on/off*, *Stop* and *Lock* call this, and closing a sheet re-renders with `fetch=true`.
2. **Tab race (stuck tab).** `switchTab()` calls `render(true)`. `render()` checks `state.tab` *before* its `await`s and draws afterwards with no check. If the user switches tabs while a fetch is pending, the old tab's HTML is drawn under the new tab's highlight, or the new tab never draws until its own 19 s fetch returns.
3. **Settings does 3 to 4 requests one after another** (`status`, then `passkeys`, then `devices`), so it is always 19 s or more.
4. **Sticky look on iPhone.** `.viz-hit:hover` and any other `:hover` rules stay "stuck" after a tap on iOS. Toggle tiles are disabled until the round trip ends, then the whole room list is re-rendered (`renderRooms()` sets `innerHTML`), which drops `:active` mid-tap.
5. **No request timeout** in `api()`, so a hung brand leaves a spinner or disabled button indefinitely.

## Step 1: backend speed (`hub/app.py`, adapters)

### L1 in-memory cache, Caffeine-style (`hub/cache.py`, the user asked for this)

A small async loading cache, modelled on Java's Caffeine. Use it for every read endpoint and adapter read instead of ad-hoc timers.

- **API:**
  - `LoadingCache(name, loader, *, max_size, expire_after_write, refresh_after_write, expire_after_access=None, timeout=None)`
  - `await cache.get(key)`
  - `cache.get_if_present(key)`
  - `cache.put(key, value)`
  - `cache.invalidate(key)` and `invalidate_all()`
  - `cache.stats()`
  - A registry (`caches: dict[str, LoadingCache]`) feeds the Admin tab.
- **Semantics (same as Caffeine):**
  - **`refresh_after_write`:** if an entry is older than this, return the old value at once and reload it in the background. At most one reload runs per key at a time.
  - **`expire_after_write`:** if an entry is older than this, the caller waits for a fresh load. If that load fails or times out, it still serves the expired value marked `stale`, instead of an error.
  - **Single flight:** concurrent `get()` calls for a missing key share one load future, so 10 tabs mean 1 adapter call.
  - **Size bound:** LRU eviction (`OrderedDict.move_to_end`) when over `max_size`.
  - **Failures:** a failed load keeps the previous value, records the error and timestamp, and backs off before the next refresh (no hammering a locked-out Rain Bird).
- **Stats, per cache:**
  - hits, misses, loads, load failures, refreshes, evictions
  - average and p95 load time
  - hit rate, size, and the age of the oldest entry
- **Uses, with starting values to tune:**

| Cache | Key | refresh | expire | Notes |
|---|---|---|---|---|
| `devices:<brand>` | brand | 10 s | 120 s | timeout 4 s; `Hub.devices()` merges every brand's cached list, so one slow brand never blocks |
| `scenes` | – | 30 s | 10 min | |
| `schedules` | – | 30 s | 10 min | invalidate on create/edit/delete |
| `status` | – | 5 s | 60 s | |
| `insights:<days>`, `energy:<days>` | days | 60 s | 15 min | invalidate after each collector run |
| `weather` | – | 30 min | 6 h | Open-Meteo 429s if hit often |

- **Write-through:** after `set_state`, update that one device inside `devices:<brand>` (`cache.put`) from the adapter's answer, or patch it optimistically and schedule a refresh of that brand in about 1.5 s.
- **Warm start:** load every `devices:<brand>` at startup, and refresh all keys every 30 s with a background ticker so first taps hit.
- **Escape hatch:** `?fresh=1` on read endpoints calls `invalidate` and then `get`.
- **Tests (`tests/test_cache.py`):**
  - single flight (10 concurrent gets, 1 load)
  - refresh-after-write returns the old value and reloads once
  - expiry waits for a fresh load
  - a failed reload keeps the old value and marks it stale
  - LRU eviction
  - the stats counts
  - Use a fake clock (inject `clock=time.monotonic`).

- (Covered by the L1 cache above; kept for detail.) **Per-adapter timeout in `Hub.devices()`:** `asyncio.wait_for(a.devices(), 4)`. If an adapter times out, use its last good devices (keep `self._last_by_brand[brand]`), mark them `stale`, and add an error such as "Rain Bird slow, showing last known".
- **Stale-while-revalidate cache:** `GET /api/devices` returns the cached list right away if it is younger than about 10 s. Otherwise it returns the cached list and kicks off one background refresh, guarded by a lock so there is never more than one refresh at a time. Add `?fresh=1` to force a wait for new data.
- A background task refreshes devices every 30 s, so the cache is always warm.
- **Find the 19 s brand** with the new per-adapter timing (step 3) and fix its root cause. Suspects: Kasa re-discovery (`No route to host` errors in the log), Sonos `soco.discover`, Hue, the water/SMT cloud, or Lennox. Rediscovery must never sit on the request path; move it to the background.
- After `POST /devices/{id}/state`, update only that device in the cache (one adapter call), not a full refresh.
- **Static files:** add `GZipMiddleware(minimum_size=1024)`. Serve `*.js`/`*.css` with `Cache-Control: no-cache` (revalidate with the ETag, which gives a 304) so updates still show up immediately.
- `/api/scenes` (0.67 s): cache for 10 s the same way.

## Step 2: front end (`hub/web/app.js`, `insights.js`, `style.css`)

- **Render generation token:** `let renderSeq = 0`. `render()` captures `const seq = ++renderSeq` and, after every `await`, returns if `seq !== renderSeq || tab !== state.tab`. This fixes the stuck-tab race.
- **Draw from memory first, then refresh:** `switchTab` draws the tab right away from `state`, using a light skeleton if empty, then fetches in the background and redraws only if still on that tab. The tab bar highlight changes on tap.
- **Settings:** fetch with `Promise.all([status, passkeys, devices])`.
- **Sheet close:** re-render from `state` (no fetch), then do a quiet background refresh.
- **Toggles:** keep the optimistic tile flip. Don't disable the whole tile; patch only that tile's DOM node (`tile.outerHTML = deviceTile(d)`) instead of `renderRooms()`. Revert with a toast if it fails. Room *All on/off*: flip the tiles optimistically, then reconcile with the server.
- **`api()`:** add an `AbortController` with a timeout (8 s for GET, 15 s for POST), and abort any in-flight GETs of the previous tab when switching.
- **CSS:**
  - Wrap every `:hover` in `@media (hover: hover)`.
  - Add `touch-action: manipulation` on buttons and tiles.
  - Add `user-select: none` on tiles and tabs.
  - Use quick `:active` feedback with no `transition` on press.
  - Tab bar: 6 columns.
- **Pull-to-refresh** or a refresh button in the header that calls `render(true)` with `fresh=1`.
- **Background refresh timer** (`setInterval` near the end of `app.js`): only when `document.visibilityState === "visible"`, never while a sheet is open, and never overlapping a refresh in flight.

## Step 3: telemetry and the Admin tab

### Backend: new `hub/telemetry.py`, no new dependency if avoidable

- **Request metrics middleware:** per route template (`request.scope["route"].path`), keep count, error count (5xx and 4xx separately), and latency samples (ring buffer of the last 500). Report p50, p95, p99 and max, plus the last 50 slow requests (over 1 s) with time, path, status and ms. Measure in the same middleware as auth, or in a second one.
- **Cache panel:** per-cache stats from `hub/cache.py` (hit rate, loads, failures, p95 load time, size, oldest entry) with an "Invalidate" button per cache.
- **Adapter metrics:** wrap `devices()`, `set_state()`, `scenes()` and `schedules()` per brand. Record calls, errors, last error text and time, last duration, p95, last success time, timeouts, and stale-cache serves.
- **Background tasks:** for the collector, recorder, irrigation, flow tests, backups and devices refresher, record last run, last duration, last result or error, and next run.
- **Event-loop lag:** a task sleeps 0.5 s and measures overshoot; keep the max and p95 over 5 min. A blocking adapter call shows up here.
- **Process:**
  - uptime, PID, Python version, hub git commit (`git rev-parse --short HEAD`, read once at start)
  - CPU % (from `os.times()` deltas)
  - RSS (`resource.getrusage` gives the peak; the current value comes from `ps -o rss= -p PID`, cached 10 s, or `psutil` if added)
  - threads (`threading.active_count()`)
  - open file descriptors (`len(os.listdir('/dev/fd'))`)
  - asyncio task count
- **Host:** `os.getloadavg()`, disk free for `~/.smarthome` (`shutil.disk_usage`), sizes of `hub.db` and its WAL file, and the size of `hub.log`.
- **Logs:** a `logging.Handler` keeping the last 200 WARNING+ records in memory (time, logger, level, message), plus counts per logger over the last hour.
- **Network:** Tailscale serve status is optional; skip it if slow.
- **Endpoints** (signed-in person only; the script token may read them):
  - `GET /api/admin/telemetry`: one JSON snapshot of everything above.
  - `GET /api/admin/logs?level=warning`.
- **Time series:** sample a few gauges (RSS, CPU %, loop lag, requests per minute, `/api/devices` p95) every 30 s into an in-memory ring buffer covering 6 h, returned in the snapshot for sparklines. Nothing goes to disk; it resets on restart, which is acceptable.
- **Tests (`tests/test_telemetry.py`):**
  - the percentile maths
  - the middleware counts requests and errors
  - an adapter timeout falls back to stale devices
  - the render race (as a backend cache test)
  - `/api/admin/telemetry` needs sign-in

### Front end: an "Admin" tab (6th tab, gauge icon), new `hub/web/admin.js`

- **Header cards:**
  - Hub up 3 h 12 m, commit `abc1234`
  - CPU %, memory MB, event-loop lag ms (with green, amber or red dots)
  - Disk free, database size
- **Requests:** a table with route, calls, p50, p95 and errors, sorted by p95, plus a sparkline of requests per minute and `/api/devices` p95.
- **Brands:** one row per adapter with a status dot, last success, last duration, p95, errors and the last error text. A Test button calls `devices()` for that brand only (`POST /api/admin/adapters/{brand}/probe`).
- **Background jobs:** last run, how long it took, result, and next run.
- **Recent warnings and errors:** the log list, newest first, filterable by brand.
- **Refresh:** auto-refresh every 5 s while the tab is visible.
- **Charts:** reuse the SVG chart helpers in `insights.js`. Respect dark mode tokens.

## Step 4: verify and ship

1. `python -m pytest -q tests` must pass (143 now, plus the new ones).
2. Restart: `launchctl kickstart -k gui/$(id -u)/local.smarthome.hub`.
3. Time `/api/devices` again; the target is under 200 ms when cached and under 4.5 s when forced fresh.
4. Screenshot Rooms, Settings and Admin in light and dark (scratchpad `shot.sh`/`preview.py` approach: 390 px iframe at `/frame?tab=`).
5. Test fast tab switching by hand on the iPhone.
6. Update `docs/HANDOVER.md`, `git fetch`, merge, then push (never force).
