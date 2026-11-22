# Handover (2026-11-21)

Where the project stands, so work can continue from another device or session.
Personal details (account email, OAuth client ID, Tailscale hostname, IPs, MACs) are
deliberately **not** in this repo; they live only in `~/.smarthome/` on the MacBook.

## What can and can't be done away from the MacBook

- **Needs the MacBook:** anything that talks to devices (they're on the home LAN), reads `~/.smarthome/`, restarts the hub, or builds the iOS app (needs Xcode).
- **Works from anywhere:** code changes, tests (`python -m pytest tests`), docs. Any device on the tailnet (the iPad is on it) can also use the web app at the hub's `https://…ts.net` address.

## Running state on the MacBook

- **Hub service:** runs as a launchd service (`scripts/install-service.sh`, label `local.smarthome.hub`). It starts at login and restarts if it stops, so no terminal or Claude session is needed. Log: `~/.smarthome/logs/hub.log`.
  - After pulling new code: `launchctl kickstart -k gui/$(id -u)/local.smarthome.hub`.
- **Access:** published only via `tailscale serve` (HTTPS); the hub listens on 127.0.0.1 only.
- **Linked brands:**
  - Working: Hue (paired, 51 lights), Kasa (4 plugs), Sonos (2 speakers).
  - Code built but **account not linked yet:** Govee, LG TV, LG ThinQ, Dyson, Ecovacs, Pentair, Schlage, Smart Meter Texas (TXU usage), McKinney water, Rain Bird. Linking happens in the web app's Settings tab, after sign-in works.

## Open items, in order

0a. **Done (2026-11-22): pool pump health, time-of-use and bills.**
   - `hub/pool.py`: the recorder stores the IntelliFlo's flow, W, speed, estimated psi and target every 2 min (`pool_samples`), at the pump's own clock; repeats are skipped.
     - Health = power at the same target flow vs. a clean baseline (the cleanest day, or the 48 h after a logged backwash/clean if lower). The shape of the rise picks the action: jump → skimmer (a), steady climb → backwash (b), backwash didn't help → clean filter (c), can't hold flow / pinned speed / power swinging → water level (d).
     - Logged fixes (`pool_maintenance`) get a before/after power comparison.
     - Also reports programs that run in paid hours, kWh free vs. paid per day, and gallons per kWh at each flow.
   - `hub/tou.py`: whole-house free (8 PM–4 AM) vs. paid split from the 15-min meter data, the paid hours just before 8 PM, and night delivery cost.
   - `hub/bills.py`: reads TXU bill PDFs (`python -m hub.bills import ~/Downloads/"TXU Invoices"` or `POST /api/bills/import`) into the `bills` table. The latest bill sets the prices (`tariff_of`); `PUT /api/tariff` overrides. Both bill layouts (plan "12" with the night delivery discount, plan "24" without) add up to the cent.
   - UI: Insights → Pool (new), and the plan, average day and bills on Insights → Electricity.
   - **Open:** Pentair's cloud stopped updating at 11:07 on 2026-11-22 (device clock `s1`); the Pool view flags readings older than 10 min. Check whether the pump only reports while the Pentair app is open. Health needs about a day of fresh readings before it shows a baseline.

0. **Done (2026-11-22): UI speed + Admin telemetry** ([plan](PLAN-UI-SPEED-TELEMETRY.md)).
   - **Device list:** `/api/devices` went from 19 s to about 10 ms (cached).
     - New `hub/cache.py` is a Caffeine-style L1 loading cache: refresh-after-write, expire with stale fallback, single flight, LRU, failure backoff, stats.
     - Device reads are cached per brand: refresh after 10 s, expire after 120 s, 4 s wait per request.
     - Scenes and schedules are cached for 30 s and expire after 10 min.
     - A warmer runs every 15 s while the app is in use, every 5 min when idle.
     - After a change, the hub patches that device in the cache and re-reads the brand 1.5 s later.
     - `?fresh=1` bypasses the cache.
   - **Admin tab:**
     - New `hub/telemetry.py` and `hub/web/admin.js`, reading `GET /api/admin/telemetry`.
     - Shows process, host, event-loop lag, per-route p50/p95, per-brand timings and errors, caches, background jobs, slow requests and a ring of warning logs.
     - Manual refresh buttons call `POST /api/admin/refresh/raw` and `POST /api/admin/cache/refresh`. They need a signed-in person; the script token can only read.
   - **Front end:**
     - Stale renders are discarded, so a tab can no longer show another tab's content.
     - The previous tab's reads are cancelled on switch; every request has a timeout.
     - Toggles flip the tile in place.
     - No sticky `:hover` on touch.
     - Header refresh button (fresh read); skeletons while loading.
   - **Known: Smart Meter Texas's first read is slow** (40 s or more, and it holds its lock during the 28-day backfill). The cache shows it as "slow to answer" instead of blocking. A possible fix in `smt_adapter` is to not hold `_lock` for `devices()` while `intervals()` runs.

1. ~~Google sign-in~~ **Done:** works on the iPhone.
   - The fix was saving the OAuth client's *Authorized redirect URIs* (origin + `/`), which had been left empty.
   - If it breaks again: `/api/auth/google/start` shows the exact `redirect_uri` the hub sends.
2. ~~Request logging~~ **Done:** back to `warning`.
3. **Link the remaining brand accounts** (Settings tab).
4. **Native iOS app (SwiftUI).**
   - **Decided:** build it. Distribution starts with free Xcode install (re-sign every 7 days); move to TestFlight if the $99 Apple Developer Program is bought.
   - **Hub side: done.** This commit adds the PKCE handoff:
     1. The app opens `https://<hub>/?app_challenge=<S256 challenge>` in `ASWebAuthenticationSession` (callback scheme `smarthome`).
     2. The user signs in with Google or Face ID as usual, then taps **Connect the iPhone app**.
     3. The page calls `POST /api/auth/app/code` and redirects to `smarthome://signed-in?code=…`.
     4. The app calls `POST /api/auth/app/token {code, code_verifier, label}` and gets an `app_…` bearer token.
     5. The app keeps the token in the Keychain behind Face ID (`.biometryCurrentSet`).
     6. `GET /api/auth/apps` lists app tokens; `DELETE /api/auth/apps/{id}` revokes one.
     - App tokens act as the signed-in user, so unlocking doors is allowed; the plain API token can lock but never unlock.
   - **To do:**
     - Write the Xcode project under `ios/` with an XcodeGen `project.yml`.
     - Screens: rooms/devices driven by `capabilities`, scenes, schedules.
     - Siri **App Intents** for devices and scenes; widgets later.
     - The hub address is entered in the app on first launch, not hard-coded.
   - **Blocker:** Xcode isn't installed on the MacBook yet (free, from the Mac App Store).
5. **Sonos native alarms and favorites.**
   - Built and tested on the branch `sonos-native-alarms` (pushed, not merged).
   - That branch predates the parallel session's Sonos adapter. Port alarms (stored on the speaker, so they run with the Mac off) and favorites onto the current `hub/adapters/sonos_adapter.py`.
6. **Muse:** the user mentioned integrating "Muse". It's unclear whether they mean the Muse meditation headband. Ask before building.
7. **Roadmap items still open** (see README): HomeKit (Leviton, Lennox), Matter (Leviton Matter, Nest Thermostat), Ring, Siri schedule Shortcut, private Alexa skill and Google smart-home project, Govee private API.

## Working notes

- **More than one Claude session pushes to `claude/brave-edison-9evhkk`.** Always `git fetch` before pushing, and never force-push.
- **Pushing from the Mac:**
  `git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push origin claude/brave-edison-9evhkk`
- **Keep personal data out of the repo.** No IPs, MACs, emails, client IDs or tailnet names; the user may share the repo.
