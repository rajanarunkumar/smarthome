# Smart home hub: handover

Read this first in a new session, then `docs/HANDOVER.md` (Mac session) and `.claude/NEXT.md`. Branch: `claude/brave-edison-9evhkk`. Last updated 2026-11-22. Tests: 143 passing.

## What this is

A self-built smart home hub for the user's home in McKinney, TX. A Python FastAPI server (`hub/`) runs on the **user's Mac**, serves a phone/iPad web app (`hub/web/`), and is reached over **Tailscale** (`tailscale serve --bg 8420`). Sign-in is Google OIDC then Face ID passkeys; scripts use a Bearer API token. The user chose this over Home Assistant. Everything private lives in `~/.smarthome/` on the Mac (`secrets.json`, `hub.db`, `photos/`), never in the repo.

The user works mostly from an **iPad/iPhone on the Claude app** (cloud session) and sometimes from a Mac desktop session. Cloud sessions **cannot reach the home network or the user's accounts**: all real devices and accounts are only reachable from the Mac hub. Everything here was therefore tested with fakes and synthetic data. **Nothing has run against the user's real devices or accounts yet.** That is the biggest open risk.

## Two sessions push to this branch

The **Mac desktop session** (runs on the user's MacBook, can reach devices) and **cloud sessions** (iPad/iPhone Claude app, cannot reach the home network) both commit to `claude/brave-edison-9evhkk`. **Always `git fetch` and merge before pushing; never force-push.** The Mac session keeps its own notes in [`docs/HANDOVER.md`](../docs/HANDOVER.md): read both. Its key facts (as of 2026-11-21):

- The hub runs on the MacBook as a **launchd service** (`scripts/install-service.sh`, label `local.smarthome.hub`), starting at login, log at `~/.smarthome/logs/hub.log`. After pulling: `launchctl kickstart -k gui/$(id -u)/local.smarthome.hub`. Dependencies must be installed into the venv the service uses (`pip install -r requirements.txt`).
- **Working on the Mac:** Hue (51 lights), Kasa (4 plugs), Sonos (2 speakers), Google sign-in on the iPhone.
- **Built but account not linked yet** (so the data analytics have no real data): Govee, LG TV, LG ThinQ, Dyson, Ecovacs, Pentair, Schlage, Smart Meter Texas, McKinney water, Rain Bird. Linking is done in the web app's Settings tab.
- **Native iOS app (SwiftUI) is planned**: hub side done (PKCE handoff `/api/auth/app/code` and `/api/auth/app/token`, app tokens `app_…` that may unlock doors); the Xcode project under `ios/` isn't written and Xcode isn't installed yet.
- Sonos native alarms and favorites exist on branch `sonos-native-alarms` (not merged; predates the current Sonos adapter).
- The user mentioned "Muse": unclear whether it means the Muse headband; ask before building.
- The Mac session refreshed the web UI (device tiles, scene filters, tab icons). The merged `style.css` keeps both its refresh and the Insights/zone/photo styles.

## Run it

```bash
python3.13 -m venv .venv && . .venv/bin/activate   # 3.13 needed for Ecovacs and Rain Bird; 3.12 for LG TV
pip install -r requirements.txt
python -m hub                       # 127.0.0.1:8420
tailscale serve --bg 8420
python -m pytest -q tests           # 143 tests, ~50 s
```

## Layout

| Path | Purpose |
|---|---|
| `hub/app.py` | FastAPI app, `Hub` class (adapters, background tasks), all `/api/*` routes |
| `hub/adapters/*.py` | One adapter per brand, subclassing `base.Adapter` (see below) |
| `hub/models.py` | Neutral `Device`, `StateIn`, `Schedule` shapes |
| `hub/scheduler.py` | Hub-run set-time schedules for devices without native schedules (never locks) |
| `hub/irrigation.py` | Smart watering programs: skip after rain, scale by evapotranspiration |
| `hub/history.py` | Watering log, water usage, weather; per-zone gal/min least squares (± range); insights |
| `hub/energy.py` | 15-min kWh, device activity, per-device kW, HVAC vs degree-days, spikes |
| `hub/weather.py` | Open-Meteo daily rain, ET0, temperatures (free, no key) |
| `hub/photos.py` | Zone photo greenness (Pillow, on the Mac), optional Claude assessment, `zone_advice()` |
| `hub/flowtest.py` | Overnight single-zone flow tests, measured gal/min |
| `hub/auth.py` | Google sign-in, passkeys, CSRF origin check, sessions |
| `hub/web/app.js` | Rooms/Scenes/Schedules/Settings tabs, device sheets, account cards |
| `hub/web/insights.js` | Insights tab: Water and Electricity views, zone cards, photos, flow tests, SVG charts |
| `tests/` | Pytest, fakes only |
| `README.md` | User-facing plan, per-brand table, setup steps, analytics explanations |

### Adding a brand
Subclass `Adapter` (`hub/adapters/base.py`): set `brand`, `label`, `prefixes`, implement `devices()`, `names()`, `set_state()`, `status()`; register in `Hub.__init__` and the `adapters` list in `app.py`; add `/api/<brand>/account` routes if it needs sign-in; add a Settings card via `accountCard(...)` in `hub/web/app.js`. Device ids are `"<prefix>:<id>"`. Brands without a rename API set `can_rename = False` (names kept in the hub DB).

## Integrations (state)

| Brand | How | Status |
|---|---|---|
| Kasa, Hue | local; native schedules on the device | original Mac-session work, user-tested on their Mac |
| Sonos | soco, local | built, untested on real devices |
| LG TV | aiowebostv (Py 3.12+), WoL | built, untested |
| Govee | LAN UDP + optional cloud API key | built, untested |
| LG ThinQ | PAT token; climate on/off; **laundry start/pause/power off/wake/delayed start, only after Remote Start is pressed on the machine** (LG API cannot pick the wash course) | built, untested |
| Dyson | libdyson-neon local MQTT; set up from the device sticker (Google-linked account, no password) or email+OTP+password | built, untested on the real device |
| Ecovacs | deebot-client (Py 3.13) | built, untested |
| Pentair Home | pypentair read + `PUT device/device-service/user/device/{id}` `{"payload":{"d25":"1"/"0"}}` pump on/off (from homebridge-pentair-cloud). Pump's own programs are undocumented; Settings → Pentair → "Pump data" shows raw fields so they can be reverse-engineered with a real pump | built, untested |
| Schlage | pyschlage cloud; **unlock needs a signed-in person, API token can only lock; locks excluded from room All off and from schedules; two-tap unlock in UI** | built, untested |
| TXU | via **Smart Meter Texas** (`smart-meter-texas`): hourly reading, 15-min intervals, 28-day backfill | built, untested |
| McKinney water | **My Water Advisor 2**, API reverse-engineered from the site JS (`customerportal-api.harmonyencoremdm.com`; login POST `/consumer/login`, header `x-access-token`; `/consumption/daily|hourly/{meterCount}/{start}/{end}`). Response shapes (`consDate`, `cons`, `units`) inferred from the JS only | built, **unverified against the live API** |
| Rain Bird | pyrainbird local (Py 3.13); run zone, stop, rain delay, run program, zone names/areas, per-zone schedule write on ESP-RZXe/ST8 with read-back. Program-based (ESP-TM2/ME3) controllers cannot be written by any library, so **smart programs** run in the hub instead | built, untested; user's controller model unknown |
| Atmos Energy | not built: no API/library; options in README | open |
| Ring | `hub/adapters/ring_adapter.py`, ring-doorbell 0.9.x; email+password then 2FA code (password kept only in memory for 10 min); saves token + hardware_id; token_updater writes refreshed tokens; doorbell/camera kinds, events, snapshot (`GET /api/ring/{id}/snapshot`), motion/light/siren actions | built, untested on real account |
| Nest | `hub/adapters/nest_adapter.py`, direct httpx to SDM; `PUT /api/nest/config` then Google partnerconnections redirect back to `/?code&state=nest.…`, web app posts `/api/nest/code`; refresh token stored; device list cached 60 s; °F in the UI, °C to Google; thermostat counted in energy TRACKED_KINDS | built, untested on real account |
| Lennox iComfort | `hub/adapters/lennox_adapter.py`, lennoxs30api 0.2.26; user uses the iComfort app login (cloud); optional IP for local. Background `_run` loop: serverConnect, subscribe each system, messagePump (cloud every 10 s, LAN long-poll, never faster than 1 s), backoff 30 s→10 min, 10 min after a rejected password. Fixed `app_id` (smarthomehub+hex) saved so Lennox sees one client. PII message logging off. Zones = thermostat devices `lennox:{sysId}:{zoneId}`; fan modes as actions | built, untested on real account |
| Leviton | not built (Matter/HomeKit) | open |

## Analytics (all in Insights)

- **Water view:** daily water split into sprinklers vs everything else, weather chart, per-zone cards with advice (increase/reduce/keep/inspect), gal/min with source label ("flow test" or "estimated ± x"), last week inches vs lawn need, leak check (1-5 AM).
- **gal/min:** least squares over hours with watering (`history.zone_flow`), plus **flow tests** (`flowtest.py`: each zone alone in its own hour after midnight, 6/night, result = hour gallons − median overnight baseline). Flow tests override estimates.
- **Lawn need:** `0.7 × ET0 − 0.8 × rain` over 7 days. Constants at the top of `history.py`.
- **Photos:** per zone; `greenness()` classes pixels green/straw on the Mac; optional Claude assessment (model `claude-opus-5-5`, structured JSON via `output_config.format`, server-side `fallbacks: "default"` with beta `server-side-fallback-2026-07-01`) if the user adds an Anthropic API key (Settings → Photo analysis). `zone_advice()` knows about winter dormancy (<60 °F mean: tan is normal) and "inspect heads" when a zone got enough water but is brown.
- **Electricity view:** per-device kW by least squares on 15-min slots, HVAC estimated from daily kWh vs CDD/HDD (65 °F), always-on watts, spikes with what was running. **Device activity only exists from the time the hub starts recording** (every 2 min); LG/Pentair keep no history.
- **Backfill:** Insights → "Fetch last 4 weeks" (also runs once at start, then every 3 h) pulls 28 days of hourly water, SMT 15-min, weather, and replays Rain Bird programs for past weeks, kept only where the water meter shows water (`confirm_with_meter`).

## Hard-won facts and decisions

- **Rain Bird connectivity** (2026-11-22, user said it is "challenging"): found by reading pyrainbird: a **lockout** (HTTP 430, "wait 15 minutes or reboot") was reported as "couldn't reach", and the 30 s poller retried a wrong password forever (extending the lockout); requests weren't serialized (the controller serves one at a time, 503 = busy). Fixed: `explain()`/`error_kind()` plain-language errors, `_watch_once` (stops after a rejected password, 16 min pause after lockout, backoff up to 5 min when unreachable), all controller calls under one lock, `diagnose()` + `POST /api/rainbird/test` + Settings "Test connection". Still unknown: the user's actual failure and controller model; whether their module has a local password at all (the newer Rain Bird app is cloud-based, and pyrainbird 6.x only has cloud *interfaces*, no usable cloud client).
- **LG ThinQ + Google sign-in:** adapter needs only a PAT, so no code change; README explains the likely need for a native LG account invited into the home (unverified). **Planned, not built:** Lennox iComfort E30 via `lennoxs30api` (LAN mode needs no account; event-driven long poll; attribute names in `lennox_zone`/`lennox_system` already inspected: `temperature`, `hsp`/`csp`, `systemMode`, `tempOperation`, `outdoorTemperature`, `humidity`, `fanMode`); Ring via `ring-doorbell` 0.9.14 (email+password+2FA, token cache, no official API); Nest via Google SDM (US$5 Device Access fee, Cloud project, OAuth redirect URI; thermostats/cameras/doorbells only, not Nest Hubs/speakers).

- **Opus review of the correlation fix** (2026-11-22, an Opus subagent reviewed it read-only; its findings were verified, not trusted): fixed and tested in `tests/test_realworld_water.py`: meter **units** now converted to gallons (CCF/cubic feet would have made every number 7.48x or 748x off with all checks green); robust `parse_stamp` (US dates, AM/PM, epoch, field-name fallbacks) with unparsed counts; hourly URL falls back to US dates if ISO is refused; running-total readings converted; unlabelled-UTC stamps converted with DST (`align_hourly` mode `utc`); iterative outlier trimming in `zone_flow` (floor 25 gal: hourly use is noisy, a lower floor trimmed normal noise) and a 40 gal/min cap; `missing_runs` replays programs for any day the hub recorded nothing; `RainBirdAdapter._read_state` falls back to zone/rain-delay requests and `watch_error` is shown in the Data check; flow tests ignore ~0 extra-water readings. **Not fixed / open:** cyclic programs can't be replayed; weekday vs weekend household baselines aren't separated; the Rain Bird combined-state claim comes from reading pyrainbird's model table, not from hardware. To settle what's real, ask the user to paste: the first 3 items of one `/consumption/hourly/...` and one `/consumption/daily/...` response (keys, `consDate`, `cons`, `units`), the Data check card, `hub.water.diagnostics`, and the Rain Bird model.
- **Water-to-sprinkler correlation** (fixed 2026-11-22 after the user asked why it wasn't captured; none of it seen on real data yet): (1) hourly water is now requested **one day at a time** (the My Water Advisor site's own JS always sends start == end; a 28-day range may be refused and used to fail silently); (2) hourly stamps with `Z`/offset are converted to the home time zone (from `location.timezone`), naive ones are taken as local; (3) `history.best_hour_shift` finds a meter clock offset by correlation and `aligned_hourly` applies it (also used for flow tests and for confirming replayed runs, detected from the unfiltered replay first); (4) `zone_flow` skips hours-of-day with fewer than 3 watering-free samples instead of falling back to an overall median (that fallback biased zone 2 by ~8% in the end-to-end test); (5) `data_checks()` + Insights "Data check" card explain which link is missing; `WaterAdvisorAdapter.diagnostics` holds the last hourly fetch result. `tests/test_correlation.py` runs the whole chain (controller poller → log → meter → gal/min) at meter offsets 0, +1 and +6.

- **S3 backups** (`hub/backup.py`, Settings → Backup): nightly 03:30 (retry hourly after a failure) tar.gz of a SQLite-online-backup snapshot of `hub.db` + `secrets.json` + optional photos, **AES-256-GCM encrypted on the Mac** (`~/.smarthome/backup.key`, excluded from the archive), uploaded with `s3:PutObject` only (SSE-S3). The user chose S3 over git for secrets because git history is permanent and the repo may be shared; never commit `~/.smarthome` or encrypted copies of it. Settings are saved only after a first upload succeeds. Restore: `python -m hub.backup restore FILE --key KEY --to DIR`. The recovery key is shown only to a signed-in person (not the script token). Untested against real S3: tests use a fake client and the real AES-GCM/tar/SQLite code. Open: Keychain-backed key storage; restore-from-S3 button (currently download the `.enc` with the AWS console/CLI).

- **Storage design** (see README "Where data is stored"): `Secrets` (`hub/config.py`) is an in-memory dict written through to `~/.smarthome/secrets.json` on every `set()`: fsync'd atomic replace, `.bak` of the last good file, auto-fallback to `.bak` if the main file is unreadable, refuses to start empty if both are bad, merges hand edits made while running. `Store` (`hub/store.py`) uses SQLite WAL + `synchronous=NORMAL`, file mode 600. `GET /api/storage` reports names/counts only. Known limit: Schlage, Smart Meter Texas, My Water Advisor and Rain Bird passwords are plain text (those services need the password). Possible next step: move secrets into the macOS Keychain (`security` CLI) and keep only references in `secrets.json`.

- **Refresh timers must use `float("-inf")` for "never"**, not `0.0`: `time.monotonic()` can be small right after boot, which skipped first refreshes. Already fixed everywhere except Kasa/Sonos/Govee-cloud list timers, which still use `0.0` (harmless there, but fix if touched).
- Rain Bird runs zones back to back, so a naive "one zone per hour" gal/min estimate fails; hence least squares and flow tests.
- Open-Meteo returns 429 when hit repeatedly in a short time. Collector runs every 3 h.
- The user's street address was given for weather; **only "McKinney, TX" is used, never stored in the repo**. Keep it that way.
- US ThinQ accounts use LG's `aic` region, not `kic`.
- Do not put secrets in the repo or in chat. Keys are pasted into hub Settings and stored in `~/.smarthome/secrets.json`.
- `README.md` changed on disk outside the session at one point (the Mac session edited it). Re-read before editing; don't overwrite blindly.
- The sandbox safety check blocked a long inline `node -` heredoc once; write scripts to a file and run them instead.
- Preview artifact (demo data only, controls nothing): https://claude.ai/artifact/ERuh96ysnPEWy9AQSNajt1. It is built by bundling `hub/web/*` with a fake API (`mock.js`); those build scripts live in the session scratchpad, not the repo. Rebuild if the UI changes.

## Open issues / next steps (priority order)

1. ~~**Dyson sign-in.**~~ Done in code: the user's Dyson is linked to a **Google account (no password)**, so Settings → Dyson now has **Add from sticker** (Wi‑Fi name `DYSON-…` + Wi‑Fi password from the device's sticker → `libdyson.get_mqtt_info_from_wifi_info`, no Dyson account). The email+OTP+password path remains for accounts that have a password. **Unverified on the real purifier**: the user needs to read the sticker and try it; if the hub doesn't find the device, check mDNS on the LAN (`_dyson_mqtt._tcp`) and that the product code on the sticker is one `libdyson.get_device` supports.
2. **Run it on the Mac and fix reality.** Pull, install with Python 3.13, restart, connect accounts in Settings, tap "Fetch last 4 weeks". Expect breakage in: My Water Advisor response shapes, Smart Meter Texas auth, Rain Bird model quirks, ThinQ profile shapes. Ask the user to paste the exact error text from the hub.
3. **Ask the user for their Rain Bird model** (front panel) to know whether per-zone schedule editing (ESP-RZXe/ST8) or smart programs applies.
4. **Pentair pump schedules:** get "Pump data" output from a real pump plus the schedule shown in the Pentair app, then work out the `zp*` fields.
5. Lennox thermostat (HVAC is the biggest electricity load; currently only estimated), Leviton, Atmos.
6. Siri/HomeKit bridge, Alexa and Google Home skills (original roadmap items 4-5).
7. Photos: consider a fixed "photo spot" reminder; no auto-capture exists.

## Where to start next

See `.claude/NEXT.md`.

## Session: Ring, Nest, Rain Bird connectivity (Nov 2026)
- Built Ring and Nest (see table). New Device kinds `thermostat`, `doorbell`, `camera`; `Device.thermostat` dict; StateIn `thermostat_mode`, `heat_f`, `cool_f`, `eco`, `fan_minutes`.
- **Web app bug fixed:** `api()` treated any 401 as "signed out" and showed the login screen, so a brand rejecting a password (Rain Bird, Ring, Nest) looked like being logged out. Now only the hub's own "Sign in required" does that.
- Rain Bird: `python -m hub.adapters.rainbird_adapter [IP] [--find]` CLI (versions, subnet hint, port probe, one password attempt; --find scans the /24). pyrainbird < 6.1 can't do HTTPS (verified by diffing wheels), so requirements now pin >= 6.10 and the connection test warns on old versions.
- `loadBackup()` no longer throws when Settings re-renders mid-load.
- Lennox iComfort added. The user's thermostat is an **E30** (supported by lennoxs30api, cloud or local).

## Session: Ask Home chat (Nov 2026)
- `hub/chat.py`: Claude (claude-opus-5-5, effort medium, server-side fallback `default`) with tools list_devices, list_schedules, control_device (runs now; `locked: false` refused), propose_changes (validated into a plan; applied only by `POST /api/chat/plans/{id}/apply`, optionally with `only` indexes). Manual tool loop on the Messages API in the hub (tools act on the LAN). Conversations in memory 1 h, append-only history; > 80 messages starts afresh. Uses the `anthropic_api_key` secret.
- Hub methods `rename_device`, `create_schedules`, `named_schedules` now back both the routes and the chat.
- UI: `hub/web/chat.js`, header button `#chat-open`; plan card with tick boxes.
- myQ: not built (Chamberlain blocks third parties). Waiting for the user's opener learn-button colour / model; plan is an ESPHome (ratgdo / Konnected blaQ) adapter with unlock-style protection for opening.
- Ask Home also runs on **Google Gemini** (`google-genai`, default model alias `gemini-flash-lite-latest`, free tier): `Chat._gemini_loop` uses the same tools via function calling with automatic calling off; model turns are appended as returned (thought signatures). Provider: `secrets['chat'].provider`, else Gemini if a key exists, else Claude. Settings `PUT /api/settings/chat`, `DELETE /api/settings/gemini`. Not yet tried against a real Gemini key; if the alias 404s, pick another model in Settings.
- Model choice: `secrets['chat'].models = {quick, thorough}` as `provider:model` from `chat.MODEL_CHOICES`; `pick_mode()` routes by keywords; per-message override via `POST /api/chat {model}`; Claude effort low (quick) / medium (thorough); Haiku sent without fallbacks. Switching provider keeps the conversation id and sends a text recap. `secrets['photo_model']` picks the Claude model for photos (`photos.PHOTO_MODELS`).
- Ask Home: **OpenAI** provider (`openai` SDK, Chat Completions function calling; `reasoning_effort` retried without for models that reject it). `PUT /api/settings/chat {openai_key}` lists the key's chat models (`openai_chat_models` filter) and stores them; choices are `openai:<id>`.
- **Admin update**: `hub/updater.py` (`GET/POST /api/admin/update`, `POST /api/admin/restart`, email session only). Fast-forward only, refuses tracked edits and diverged branches, pip install when requirements.txt changed, restart = SIGTERM under launchd (`XPC_SERVICE_NAME == local.smarthome.hub`, KeepAlive restarts it) else `os.execv`. UI in `admin.js` (`openUpdate`), reloads the page once `/api/health` answers.
- Electricity fix (user: home is ~all LED; two incandescent chandeliers 8-11 PM; office insights made no sense): `device_power` now only solves MEASURABLE_KINDS (appliance, pool, plug, thermostat), drops estimates below 0.15 kW, above a per-kind max, or within 2 standard errors of zero, and uses owner-entered watts (`device_watts` table, `PUT /api/energy/watts/{id}`) exactly, subtracting them first. Lights are recorded in device_activity only once watts are entered. Status per device: measured / entered / small / learning.
- Admin: top-level **Restart hub**, **Log** viewer (`GET /api/admin/logfile?lines=&after=` tail + follow, secrets masked by `LOG_SECRETS`).
- `Device.watts` = live power the device measures (Kasa `Module.Energy`, Pentair `s18`). `EnergyStore.observe` averages it while on (`device_reported`, ≥2 W, ≥3 samples); `known_watts()` = reported overridden by entered; status `reported`.
- Bulb power: `hub/lights.py` + `hub/data/light_power.json` (Powercalc, MIT, licence in `hub/data/LIGHT_POWER_LICENSE`, rebuilt by `scripts/build_light_power.py`). `Device.model_id` (Hue product_data.model_id). Lights sample model×brightness watts into `device_estimated`; priority estimated < reported < entered; status `rated`; standby total in recommendations.
- Always-on appliances (user has: GE upright freezer, Sub-Zero built-in, 2 beverage coolers): `fixed_loads` table, `APPLIANCE_TYPES` typical kWh/yr, `/api/energy/appliances` CRUD; `series[].appliances_kwh`, subtracted before `hvac_model`. Not yet entered on the user's hub.
- Always-on load (user's idea): `quiet_baseline()` = median of weekday 12–4 PM 15-min readings, excluding thermostat/unknown big-load slots, subtracting known watts; pre-thermostat days only if mild (58–68 °F), else lower quartile. Shown in the Always-on tile and compared with listed fixed loads (warns if they exceed it).
- User saw CPU steady ~16% of a core. Added `hub/profiler.py` (`POST /api/admin/profile`, Admin → What's using CPU?). Suspects before measuring: cache warmer refreshing every active brand every 15 s for 10 min after any app use (`Hub._warm_loop`), Lennox cloud poll every 10 s, library threads (libdyson MQTT, soco events, zeroconf). Waiting for the user's profile result.
