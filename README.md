# smarthome

A self-built hub that controls every smart device in the house from **one iPhone app**, while **Alexa, Google Home and Siri** keep working as voice front ends.

## Approach

```
 iPhone web app ─┐                         ┌─► Hue Bridge     (local API; schedules run on the bridge)
 Siri Shortcuts ─┤                         ├─► Kasa plugs     (local protocol; schedules run on the plug)
 Alexa skill ────┼─►  hub on the MacBook  ─┼─► Leviton, Lennox (HomeKit, local)      ┐
 Google project ─┘    (Python, this repo)  ├─► Leviton Matter, Nest Thermostat (Matter)├ planned
                                           ├─► LG TV, Sonos   (local)                ┘
                                           └─► Govee, Smart Life, Ring, Nest cams (cloud APIs)  planned
```

Decisions so far:

- **Self-built, no extra hardware or paid services.** The hub is our own code; each brand's protocol comes from a well-maintained open-source library (`python-kasa`, Hue's local API, `aiohomekit`, `python-matter-server`, `aiowebostv`, `soco`, `ring_doorbell`).
- **The hub is the source of truth for names and rooms.** Renaming a device in the app writes the name back to the brand (so the Hue and Kasa apps show it too). Rooms live in the hub; Hue rooms are imported on first pairing.
- **Schedules are stored in each brand's own system** wherever its API allows it, so they keep running when the MacBook is asleep and show up in the brand's app. The hub also reads schedules made in the brand apps, so everything appears in one list.
- **Live state is two-way.** Changes made from a brand app, a wall switch or a voice assistant show up in the hub's app within seconds.
- **Voice:** Alexa and Google Home reach the hub through our own private Alexa skill and Google smart-home project (planned). Siri reaches it through a Shortcut and, later, a HomeKit bridge. Ring stays linked to Alexa directly through Amazon's own Ring skill.

### Where each brand's schedules can live

| Brand | Native schedules from the hub | Runs on |
|---|---|---|
| Hue | ✅ Bridge schedules (hub-created); Hue app automations listed and can be switched on/off | Hue Bridge |
| Kasa | ✅ Rules on the plug (verified on the HS103 and KP405) | The plug |
| Sonos | ✅ Alarms (planned) | The speaker |
| Smart Life (Tuya) | ⚠️ Cloud timers, needs a free Tuya developer project | Tuya cloud |
| Lennox E30 | ⚠️ Can switch between the thermostat's schedules | Thermostat |
| Govee | ❌ Not in the public API; investigating the app's private API | Hub |
| Leviton, LG TV | ❌ No API | Hub |
| Ring | Modes (Home/Away/Disarmed) only, via the unofficial library | Hub |
| Nest Thermostat | ❌ Keep Nest's own schedule; the hub can change temperature and mode | Thermostat |

## Your devices

| Brand | How the hub connects | Local? | Matter option | Notes |
|---|---|---|---|---|
| Philips **Hue** | Bridge local API (CLIP v2), paired with the link button | ✅ | Bridge v2 supports Matter | **Built.** Colors, white temperature, brightness, Hue scenes (including dynamic), schedules. |
| TP-Link **Kasa** | `python-kasa`, local protocol, no login | ✅ | KP125M / EP25-class plugs | **Built.** On/off, dimming (KP405), schedules. |
| **Leviton** Decora Smart | 2nd-gen Wi-Fi models (D215S, D26HD): HomeKit, local, using the 8-digit code on each device. Matter models: share them from Alexa ("pair with another app") without resetting | ✅ | Yes | Planned. The five HomeKit ones are unpaired. |
| **Govee** | LAN API (turn on *LAN Control* per device) for color; cloud API key for Govee's own scenes | ✅ / ☁️ | Newer models | Planned. None answered the scan yet. |
| **Smart Life** (Tuya) | Tuya cloud API; local keys optional | ☁️ / ✅ | Rare | Planned. Replace with Matter devices over time. |
| **LG** (ThinQ appliances, webOS TV) | `aiowebostv` for the TV (accept a prompt on the TV); ThinQ personal access token for appliances | TV ✅ / appliances ☁️ | Some newer ThinQ | Planned. |
| **Sony** (Bravia TV) | Bravia IP control with a pre-shared key set on the TV | ✅ | – | Planned. Not seen in the scan; it was probably off. |
| **Lennox** iComfort E30 thermostat | HomeKit (unpaired; code in the thermostat's settings) | ✅ | – | Planned. |
| **Sonos** (Beam, Amp) | `soco`, local | ✅ | – | Planned. |
| **Ring** (doorbells) | Unofficial `ring_doorbell` library (Ring login + 2FA once, refresh token kept on the hub) for events, snapshots, lights, siren, alarm modes | ☁️ | – | Planned. **Stays linked to Alexa** through the Ring skill for announcements and live view. |
| **Nest** (thermostat, cams, doorbell, Protect) | Thermostat: Matter, shared from Google Home. Cams and doorbell: Google Device Access ($5 one-time). Protect: no API | ✅ / ☁️ | Thermostat | Planned. |
| **Amazon Echo** | Voice front end. Hub reaches Alexa through its own private skill | ☁️ | Matter controller; two Echos are Thread border routers | Leviton Matter devices are paired here today. |
| **Google Nest** speakers and displays | Voice front end. Hub reaches Google through its own smart-home project | ✅ | Matter controller | |
| **Apple TV** 4K | Home hub for Siri and Apple Home when the hub exposes a HomeKit bridge | ✅ | Matter/HomeKit home hub | |

## Scan results

Scanned from a Mac on the home Wi-Fi on 2026-11-21 (two passes plus the ARP table). Names are the device names from each app.

| Brand | Devices found | Matter / HomeKit |
|---|---|---|
| Philips Hue | Hue Bridge (v2) | Matter: already shared with Apple Home, Google Home and one more controller |
| TP-Link Kasa | HS103 plugs: *Library*, *Library 2*, *Office*. KP405 outdoor dimmer plug: *Jazz* | – |
| Leviton Decora Smart | 3 × D215S switch and 2 × D26HD dimmer (Wi-Fi). 3 more Leviton devices running Matter | HomeKit (unpaired) on the five Wi-Fi devices. Matter (in Alexa) on the other three |
| LG | OLED65C1PUB webOS TV: *living room tv* | HomeKit (unpaired), AirPlay |
| **Lennox** (new) | iComfort E30 thermostat | HomeKit (unpaired) |
| **Sonos** (new) | Beam: *Bedroom*. Amp: *Family Room* | AirPlay |
| **Amazon** (new) | 3 Echo speakers (2 are Thread border routers), plus 1 Echo or Fire TV | Matter controller |
| **Google Nest** (new) | Nest Hub: *Bathroom Display*, *Dining Room Display*. Google Home: *Upstairs speaker*. Nest Mini: *Hallway speaker*. 3 more Google devices on Matter | Matter controller |
| **Apple** (new) | Apple TV 4K: *Gaming room* | Matter/HomeKit home hub |
| Other | Netgear Orbi router, Epson ET-2800 printer | – |
| Unidentified | 1 device with an Espressif Wi-Fi chip (often Govee or Smart Life), 1 Apple device, 4 devices with private (randomized) MACs, likely phones/laptops | – |

**Not seen:** Govee, Smart Life (Tuya), Sony Bravia and LG ThinQ appliances. Govee answers only after *LAN Control* is turned on per device in the Govee app. Smart Life and ThinQ are cloud-only. The Bravia was probably off.

## The hub (phase 1: built)

`hub/` is a Python server (FastAPI) with a web app for the iPhone. It currently covers **Kasa**, **Hue**, **Sonos**, **LG webOS TV** and **Govee** over the local network, plus accounts for **Govee**, **LG ThinQ**, **Dyson**, **Ecovacs** and **Pentair Home**:

| Brand | What the hub can do | Setup |
|---|---|---|
| Kasa | On/off, brightness, rename, schedules on the plug | None: found automatically |
| Hue | On/off, brightness, color, white, rename, scenes, schedules on the bridge | Settings → Pair (press the bridge button first) |
| Sonos | Play/pause, next/previous, volume, mute, rename. The switch on the Rooms tab means play/pause. Grouped speakers play and pause together. | None: found automatically |
| LG TV | On/off, volume, mute, input (HDMI…) or app, play/pause | Settings → LG TV → Connect, then **Accept** on the TV. For turning the TV *on*, enable **Turn on via Wi‑Fi** on the TV (Settings → General → Devices → TV Management / External Devices). Needs Python 3.12+. |
| Govee | On/off, brightness, color, white | In the Govee app, turn on **LAN Control** for each light (device → settings). Not every model supports it. |
| Govee account (optional) | Adds Govee lights without LAN Control, Govee plugs, and the device names from the Govee app. A light that answers on the LAN is still controlled locally. | In the Govee Home app: Profile → Settings → **Apply for API Key** (arrives by email). Paste it in hub **Settings → Govee account**. |
| LG ThinQ (optional) | Washer, dryer, fridge, dishwasher, oven and other ThinQ appliances, with status such as "Running · 32 min left". AC, air purifiers, (de)humidifiers and fans turn on and off. **Washers and dryers:** start, pause, power off, wake up and delayed start. Starting only works after **Remote Start** is pressed on the machine (LG's safety rule). LG's API can't choose the wash course, so pick it on the machine before pressing Remote Start. Dishwashers and ovens are read-only. | At **connect-pat.lgthinq.com**, sign in with your LG account, **Add new token** with every permission ticked, and paste it in hub **Settings → LG ThinQ appliances** with your country code. |

| Dyson | Purifiers and fans: on/off, fan speed, auto, oscillation, night mode, temperature, humidity, PM2.5 and filter life. Controlled directly on the Wi‑Fi. | Settings → **Dyson**. **Google or Apple sign-in (no Dyson password):** use **Add from sticker**: the Wi‑Fi name (`DYSON-AB1-US-ABC1234A-438`) and Wi‑Fi password printed on the sticker on the device or in its manual; no Dyson account is involved. **Account with a password:** enter the MyDyson email, then the code Dyson emails and the password. Only each device's local key is saved. |
| Ecovacs | Deebot robots: clean, pause, resume, return to dock, battery. | Settings → **Ecovacs** with the Ecovacs app email and password (only a hash is saved). Needs Python 3.13+. |
| Schlage | Encode Wi‑Fi locks: lock, unlock, who used it last, door open/closed, battery. **Safety:** locks have no on/off switch and are skipped by a room's "All on/All off"; unlocking needs a second tap and a person signed in with Google or Face ID. Scripts using the API token (Siri Shortcuts) can lock but never unlock. | Settings → **Schlage** with a Schlage Home email and password. Schlage's sign-in needs the password, so it's kept in `~/.smarthome/secrets.json`. Share the lock with a **second Schlage account** in the Schlage Home app and use that here, so you can revoke the hub on its own. |
| Pentair Home | Pool equipment readings, and **IntelliFlo pumps on/off** (the `d25` command the open-source homebridge-pentair-cloud plugin uses; Pentair doesn't publish its API). Pump schedules run in the hub (below). The pump's own programs from the Pentair app aren't documented anywhere; Settings → Pentair → **Pump data** shows the raw fields so they can be worked out from a real pump. | Settings → **Pentair** with the Pentair Home email and password (only login tokens are saved). |
| TXU (Smart Meter Texas) | Electricity: yesterday's kWh (from 15-minute data) and the meter reading, read hourly (Smart Meter Texas allows about 2 reads an hour). TXU has no API of its own; it reads the same meter. | Make a free account at **smartmetertexas.com** (separate from TXU), then Settings → **Smart Meter Texas**. |
| McKinney water (My Water Advisor 2) | Water: latest full day, last 7 days, this month. Read only. Uses the same calls as the mywateradvisor2.com website, which has no published API, so a site change can break it. | Settings → **McKinney water** with the My Water Advisor email and password. |
| Rain Bird | Controllers with the LNK/LNK2 Wi‑Fi module, controlled directly on your Wi‑Fi: run any zone for 5–60 minutes, stop, rain delay (1–14 days), what's watering and time left, rain sensor, and the controller's own programs (read only). Zone names are kept in the hub. Needs Python 3.13+. | Settings → **Rain Bird** with the controller's IP address and the **controller password** set in the Rain Bird app (not your Rain Bird account password). |
| Ring | Doorbells and cameras (indoor and outdoor): latest ring and motion, the event history, a fresh **snapshot**, motion detection on/off, floodlight and siren where the camera has them, battery and Wi‑Fi signal. No live video. Uses Ring's cloud through the unofficial `ring-doorbell` library (the one Home Assistant uses). | Settings → **Ring** with the Ring email and password, then the code Ring texts or emails. Only Ring's token is saved, never the password. The hub appears in the Ring app under Control Center → Authorized Client Devices, where it can be removed. |
| Lennox iComfort | **S30, E30, M30 and S40** thermostats: temperature, humidity, heating or cooling now, mode (Heat, Cool, Heat·Cool, Off), set temperatures, fan (auto, on, circulate), outdoor temperature. Each zone of a zoned system is its own thermostat. Run time feeds the Electricity insights. Uses `lennoxs30api` (the library Home Assistant's Lennox integration uses). Older iComfort Wi‑Fi thermostats (myicomfort.com, before the S30) aren't supported. | Settings → **Lennox iComfort** with the iComfort app's email and password. Lennox's cloud asks for the password at every reconnect, so it's kept on the Mac (invite a second account to the home in the iComfort app if you want to revoke the hub on its own). **Or** fill in only the thermostat's IP address to connect on your Wi‑Fi with no account (thermostat → Settings → Advanced → About shows it; reserve it in your router). |
| Nest thermostat | Temperature, humidity, heating/cooling now, mode (Heat, Cool, Heat·Cool, Off), set temperatures, Eco, fan timer. Google's official Smart Device Management API. Thermostat run time also feeds the Electricity insights. | See **Nest thermostat** below (one-time US$5 Google fee). |
| Atmos Energy | Not supported yet: Atmos has no API, no library, and isn't on Opower. | See "Atmos Energy" below. |

Govee, LG TV, LG ThinQ, Dyson, Ecovacs, Pentair, Schlage, Ring, Nest and Lennox have no way to rename a device from outside their apps, so names you give those in the hub are kept in the hub. Account keys are saved only in `~/.smarthome/secrets.json` on the Mac and are never sent back to the web app.


- **Rooms tab:** every device grouped by room, power switches, "all on/off" per room. Tap a device to rename it (the name is written back to the brand), move it to a room, or set brightness, color and white temperature.
- **Scenes tab:** your Hue scenes by room, including dynamic scenes.
- **Schedules tab:** one list of every schedule, whether made here or in the brand's app, labelled with where it runs. New schedules are saved on the plug or bridge itself.
- **Settings tab:** Hue Bridge pairing and room management.

### Run it

```bash
python3.13 -m venv .venv && . .venv/bin/activate   # brew install python@3.13. Older Python works without LG TV (needs 3.12) and Ecovacs (needs 3.13)
pip install -r requirements.txt
python -m hub            # listens on this Mac only (127.0.0.1:8420)
tailscale serve --bg 8420   # once: publishes it at https://<mac>.<tailnet>.ts.net to your own devices
```

To keep it running, install it as a background service instead of running `python -m hub` by hand. It starts at login and restarts within about 10 seconds if it stops; the log is `~/.smarthome/logs/hub.log`.

```bash
scripts/install-service.sh            # install or update, then start
scripts/install-service.sh uninstall  # stop and remove
```

After pulling new code, restart it with `launchctl kickstart -k gui/$(id -u)/local.smarthome.hub`. The Mac must be awake for the app to reach it: keep it plugged in (it doesn't sleep on power) with the lid open.

On the iPhone (Tailscale on), open the `https://…ts.net` address in Safari, **Sign in with Google**, accept **Use Face ID next time**, then **Share → Add to Home Screen**. To pair Hue, press the round button on the Hue Bridge, then tap **Settings → Pair** within 30 seconds.

### Sign-in

- **Google** (OpenID Connect, redirect flow) for the first sign-in on a device. Only allow-listed Google accounts get in.
- **Face ID / Touch ID** passkeys after that. Sessions last 12 hours, then one Face ID tap.
- **API token** (`Authorization: Bearer …`) for scripts such as Siri Shortcuts, which can't do Face ID.
- **iPhone app** (planned, native): signs in through this page once (PKCE handoff), then keeps its own revocable token in the Keychain behind Face ID. See `docs/HANDOVER.md`.
- Requests that change anything must come from one of the hub's own addresses (CSRF protection).

Settings live in `~/.smarthome/secrets.json` under `auth`: `google_client_id`, `allowed_emails`, `origins` and `session_hours`. In Google Cloud, the OAuth client (type *Web application*) needs each origin as an **Authorized JavaScript origin** and each origin plus `/` as an **Authorized redirect URI**.

Private data lives in `~/.smarthome/` (sign-in settings, API token, Hue key, sessions, passkeys and the rooms database), never in this repo.

Tests: `pip install -r requirements-dev.txt && python -m pytest tests`.

## Where data is stored

Everything lives on the Mac running the hub, in `~/.smarthome/` (owner-only, mode 700). Nothing is stored in the repo, on GitHub, or in the Claude cloud session. Settings → **Stored on this Mac** shows this live (names and counts, never values).

| File | Holds | Notes |
|---|---|---|
| `secrets.json` (+ `.bak`) | Every setting and login: hub API token, Google sign-in config, Hue/LG/Govee/ThinQ/Dyson/Pentair keys, Anthropic key, weather city, and the logins for Schlage, Smart Meter Texas, My Water Advisor and Rain Bird (**plain-text passwords**, because those services accept only a password; Ecovacs keeps a hash) | In-memory store written through to disk on every change; crash-safe save (temp file, fsync, swap), last good copy kept as `.bak`, loaded automatically if the file is ever unreadable, and changes made to the file by hand are merged, not overwritten. Mode 600 |
| `hub.db` (SQLite, WAL mode) | Rooms and names, sessions and passkeys, hub schedules, smart programs and decisions, flow tests, sprinkler runs, water, weather, 15-minute electricity, device activity, photo records | Crash-safe write-ahead log; mode 600 |
| `photos/` | Zone photos (JPEG, max 2048 px) | Mode 700 folder |
| `logs/hub.log` | Hub log (launchd service) | |

Not stored: device state (it's read live from the devices and cached in memory only), Dyson/Schlage/other account sessions beyond the keys above.

Backups: `~/.smarthome/` is the whole system. Copy it to restore on another Mac (stop the hub first). It contains passwords, so keep the copy private and encrypted; turn on FileVault. Don't commit it.

## Backing up to your own S3 bucket

The hub can back up `~/.smarthome/` every night at 3:30 AM to a bucket you own. The archive (a consistent snapshot of `hub.db`, `secrets.json`, optionally the zone photos) is **encrypted on the Mac with AES-256-GCM before upload**, so S3 only ever holds ciphertext. The key is created on first use and kept in `~/.smarthome/backup.key`, never inside a backup.

**Set it up once (about 10 minutes):**
1. **Bucket:** create a private bucket. Leave **Block all public access** on, turn on **Bucket Versioning**, and keep default encryption (SSE-S3). Add a **lifecycle rule** that expires objects under `smarthome/` after 30 days, because the hub never deletes anything.
2. **IAM user** for the hub with only this policy (replace `YOUR-BUCKET`):
   ```json
   {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:PutObject","Resource":"arn:aws:s3:::YOUR-BUCKET/smarthome/*"}]}
   ```
   It can add backups but can't list, read or delete them, so a leaked hub key can't expose or destroy your history. Create an access key for that user.
3. In the hub: **Settings → Backup to your S3 bucket**: bucket, region, folder (`smarthome`), the access key ID and secret (or leave them blank to use the Mac's `aws configure` login), then **Connect and back up now**. The settings are saved only if the first upload works, which proves the permissions.
4. **Save the recovery key** that the app shows (also under *Recovery key*) in your password manager. Without it a backup can't be opened, and if the Mac dies the key file dies with it.

**Restore on a new Mac:** download a `.enc` file from S3, stop the hub, then
```bash
python -m hub.backup restore hub-20261122-033000.enc --key "<recovery key>" --to ~/.smarthome
```
It refuses a folder that isn't empty (add `--force` to overwrite) and rejects anything in the archive that would land outside the target. Afterwards copy the recovery key back to `~/.smarthome/backup.key` if you want new backups to keep using it.

Notes: the AWS secret key is stored in `secrets.json` like the other logins (or use `aws configure` so it isn't stored by the hub). The archive contains your plain-text service passwords, which is why it's encrypted before it leaves the Mac. Don't put the recovery key in the same bucket.

## LG ThinQ with a Google (or Apple) sign-in

The hub only ever needs a **token**, never your LG password, so there is nothing to change in the hub itself. The question is whether LG's token page lets you in:
1. Open **connect-pat.lgthinq.com** and choose the same sign-in you use in the ThinQ app (for example **Continue with Google**) if it's offered.
2. Community reports say that page may accept only an LG email-and-password account (LG doesn't confirm this either way). If so: create an ordinary LG account (email + password), **invite it as a member of your home in the ThinQ app** (the menu name varies by app version), and make the token with that account. I haven't verified that appliances shared this way show up in LG's API, so check the Settings card says how many appliances it found.

## What the hub does in the background

Work follows use. *In use* means someone made a request in the last 10 minutes (the app open on any device, or a Shortcut).

| | While the app is in use | When it isn't |
|---|---|---|
| Keeping screens current | local devices every 15 s, cloud brands every 60 s | nothing; the next visit shows the last state at once and refreshes |
| Recording for insights | every 2 min from what the screens already loaded | thermostats every 2 min (in memory, free); the brands that feed insights every 10 min; others not at all |
| Lennox (iComfort cloud) | every 10 s | every 60 s |
| Rain Bird (sprinkler runs) | every 30 s | every 60 s |
| Water, electricity, weather collection | every 4 hours, or *Fetch last 4 weeks* | every 4 hours |
| Insights | computed when opened, then reused until new data arrives or something is changed (up to 4 h; HVAC and pool 2 min, they show live readings). The refresh button recomputes. | nothing |

Schedules (hub, irrigation, flow tests) and the nightly backup keep running on time regardless.

## Updating and restarting (Admin)

**Admin → Update or restart…** shows the branch and commit the hub runs, what's new on GitHub, and every branch. **Pull and restart** fetches, switches branch if you picked another, fast-forwards, installs packages if `requirements.txt` changed, and restarts; the page reloads when the hub is back (about 10 seconds). **Pull only** skips the restart; **Restart** just restarts. It never merges or throws anything away: if the Mac's copy has edits or commits of its own, it stops and says so. Needs a person signed in (scripts with the API token can't update or restart). Restarts rely on the background service from `scripts/install-service.sh`; without it the hub replaces its own process. The Mac needs to be able to `git fetch` without a password prompt (the macOS keychain usually handles this after one manual `git pull`).

**Admin → Restart hub** restarts on its own (about 10 seconds; the page reloads when it's back). **Admin → What's using CPU?** watches the hub for 15 seconds (sampling every thread's stack, no extra packages) and lists which code was busy, by hub module and library. **Admin → Log** shows the last 1000 lines of `~/.smarthome/logs/hub.log` and follows new lines every 2 seconds, with a filter box; anything that looks like a password or token is masked.

## Electricity: lights and entered watts

The meter reports every 15 minutes, and normal swings (the AC, fridge, water heater) are a few hundred watts, so devices smaller than that can't be measured from it. The hub therefore only estimates the big loads (washer, dryer, pool pump, AC, smart plugs) and keeps an estimate only when the data clearly supports it. LED lights, TVs, purifiers and robot vacuums show as *too small to measure* instead of a made-up number. For a big load the meter can't single out, such as an incandescent chandelier, enter its watts: in Insights → Electricity → *Set W*, or in the device's panel → *Power when on*. Entered devices are counted exactly (watts × time on) and taken out before the others are estimated. Devices that measure their own power are used automatically, no typing needed: Kasa plugs with energy monitoring (KP115, KP125/KP125M, EP25, HS110 and similar) and the Pentair IntelliFlo pump. The hub averages what they report while on; a number you enter still wins. Leviton dimmers, LG, Sonos, Dyson and the thermostats don't report power.

**Smart bulbs** (Hue, Govee, Kasa) don't measure power either, but every bulb reports its model, and bulbs of one model draw the same. The hub looks the model up in a table of measured power at each brightness (from the open-source [Powercalc](https://github.com/bramstroker/homeassistant-powercalc) profile library, MIT licence, 578 models), scales it by the brightness each bulb is set to, and shows it in the device list as *bulb model*. Models it doesn't know use the published rating or a typical LED figure (9 W bulb, 20 W strip), shown as *typical bulb*. It also adds up what the bulbs draw while switched off (about 0.3–0.5 W each, to stay on the network). Dimmers don't say what's connected to them, so enter watts for those (the incandescent chandeliers). To refresh the table: `python scripts/build_light_power.py <powercalc>/profile_library hub/data/light_power.json`.

**Always-on appliances** (Insights → Electricity → *Always-on appliances*): fridges, freezers and beverage coolers aren't connected but run all the time, so add each one with the kWh per year from its yellow EnergyGuide label (or the spec sheet). Without a figure a typical one is used: upright freezer 534 kWh (a GE 20 cu ft upright's rating), refrigerator 600 (Sub-Zero built-ins run about 300 without a freezer to 640 with one), beverage cooler 300 (no ENERGY STAR category; often inefficient). They appear as *Fridges and freezers* in the daily chart, come out of *Everything else* before heating and cooling is estimated, and their share of the always-on load is shown.

**The always-on load** is measured on **weekday afternoons, 12–4 PM**, when nobody's cooking, washing or watching TV. It's the typical (median) reading over the last weeks, which captures fridges and freezers at their average cycling. The overnight minimum catches them all off and reads low. Readings when the AC or furnace ran, or a big tracked device (dryer, pool pump without its own power figure), are left out; bulbs and devices that report power are subtracted. Before a thermostat was connected, only mild days (58–68 °F) are used, so the AC can't creep in. Insights then compares it with the fridges and freezers you listed and says what's left for network gear and standby electronics, or that the listed figures are too high. Example: two chandeliers with eight 60 W bulbs each = 960 W.

## Ask Home (chat)

The speech-bubble button at the top opens a chat. Ask in your own words: "turn off everything downstairs", "set the thermostat to 70", "run the front lawn for 10 minutes", "what's on right now?", "review my device names", "review my schedules".

- **Devices change straight away**, through the same path as tapping in the app. Unlocking is never possible from chat (locking is).
- **Names, rooms and schedules come back as a plan.** Each change shows with its reason and a tick box; nothing changes until you tap **Apply**, and you can untick any you don't want.
- **Which model answers** (Settings → **Ask Home**):
  - **Google Gemini, free tier.** Get a key at aistudio.google.com → *Get API key* (no card needed). The default model is Flash-Lite, which has the largest free allowance; Flash is smarter but allows far fewer free requests a day. A command takes about 3 requests. When the free limit runs out, the chat says so; it resets daily (midnight Pacific). On the free tier Google may use what's sent, including your device names and rooms, to improve its products.
  - **Claude**, using the Anthropic key in **Settings → Photo analysis**. Pay as you go: roughly 5–10¢ per command and 15–30¢ for a full review of names or schedules on Claude Opus 5.5.
  - **Pick a model per kind of request** in Settings → Ask Home: one for *everyday commands* and one for *reviews and questions*. The chat decides which kind each message is from its wording ("review", "tidy", "suggest", "why"…), and the **Model** menu at the top of the chat overrides it for the next messages. Each reply shows which model answered. With only one provider's key, sensible defaults are used (Gemini: Flash-Lite for commands, Flash for reviews; Claude: Haiku for commands, Opus for reviews).
  - **Photo analysis** has its own choice of Claude model (Haiku is cheapest, Opus the most careful).
  - **OpenAI** too: add a key from platform.openai.com → API keys (paid, needs credit on the account). The hub asks OpenAI which chat models the key can use and offers those.
  - The conversation is kept in the hub's memory for an hour, not on disk. Moving between Gemini and Claude mid-conversation carries a short recap of the last few exchanges over.

**Naming that works well everywhere** (the chat follows this when you ask it to review names): *Room + what it is* ("Kitchen Island Lights", "Primary Bedroom Fan"), 2–4 words, unique, no brand or model names, a plug named after what's plugged into it, and nothing that sounds like another device or a command. These names carry over to Alexa and Google Home once they're connected.

**Schedules:** prefer sunset/sunrise with an offset for lights, look for duplicates and on/off pairs that clash, and remember that schedules for brands without their own scheduling run in the hub (keep the Mac awake).

## Garage door (myQ)

Chamberlain closed myQ to other apps in 2023, and the workarounds keep breaking, so the hub doesn't use the myQ cloud. The reliable route is a small board wired to the opener that the hub controls on your Wi‑Fi: **ratgdo** (about $40–60) or **Konnected GDO blaQ**. They work with Security+ 2.0 (yellow learn button) and older Chamberlain/LiftMaster openers, not Security+ 3.0. Planned: open/close/light/obstruction, opening protected like unlocking (second tap, signed-in person, never from scripts or chat).

## Nest thermostat

Google only lets outside apps control a Nest through **Device Access**, which needs a one-time US$5 registration. About 15 minutes, once:

1. **Register** at [console.nest.google.com/device-access](https://console.nest.google.com/device-access) with the Google account that owns the thermostat, and pay the US$5 fee.
2. **Google Cloud project** (the one used for Google sign-in is fine): in [console.cloud.google.com](https://console.cloud.google.com), *APIs & Services → Library*, enable **Smart Device Management API**.
3. **OAuth client:** *APIs & Services → Credentials → Create credentials → OAuth client ID → Web application*. Under *Authorized redirect URIs* add the hub's address with a trailing slash, exactly as Settings → Nest shows it (for example `https://your-mac.your-tailnet.ts.net/`). Copy the **client ID** and **client secret**. On *OAuth consent screen*, set the app to **In production** (in Testing, Google expires the link after 7 days); you can skip verification for your own account.
4. **Device Access project:** in the Device Access console, *Create project*, paste the OAuth client ID, skip events. Copy the **project ID** (a UUID).
5. Hub: **Settings → Nest**, paste the project ID, client ID and client secret, tap **Save and link**. Google asks which home and thermostat to share; tick the thermostat. You come back to the hub and it appears in Rooms.

The hub reads the thermostat at most once a minute (Google limits calls); temperature taps are sent once you stop tapping. "Google no longer accepts the hub's Nest link" means the link was removed or expired: tap **Link again**.

## Ring

Settings → **Ring**: email and password, **Continue**, then the code Ring sends. If your Ring login has no password (a sign-in through another account), set one first: ring.com → Forgot password, with the same email. For a separate revocable login, add a **Shared User** in the Ring app (Control Center → Shared Users) and use that account here; shared users see devices and events but can't change some settings. Snapshots from battery cameras need **Snapshot Capture** on in the Ring app (Device Settings → Video Settings).

## Rain Bird: when it won't connect

**If the password works in the Rain Bird app but not in the hub,** the app is almost certainly not talking to the controller the way the hub does. The app also works through Rain Bird's cloud, from anywhere; the hub needs to reach the controller **directly on your network**. The usual causes, most likely first:

1. **The IP address changed** (routers hand out new ones). Run the finder on the Mac, from the smarthome folder: `.venv/bin/python -m hub.adapters.rainbird_adapter --find` (the hub's venv). It asks for the controller password, searches the Mac's network and prints the controller's address. Reserve that address for the controller in your router so it stops moving.
2. **The controller is on a separate IoT or guest network** (Orbi, eero and others offer one). The app still works via the cloud, but the Mac can't reach it. Put the controller on the main network, or the Mac on the same one.
3. **Old Rain Bird library.** pyrainbird before 6.1 speaks only HTTP; newer LNK2 firmware answers only HTTPS, so the right password still fails. `pip install -U -r requirements.txt` in the hub's venv, then restart the hub. The test reports the installed version.
4. **Wrong kind of password.** It's the controller's own password (Rain Bird app → the controller → Settings → Controller password), not the Rain Bird account password.

The same test from the command line, without the web app: `.venv/bin/python -m hub.adapters.rainbird_adapter 192.168.1.40` (your controller's address). It prints Python and library versions, which network this Mac is on, whether the ports answer, and what the controller says. It sends one password attempt.

**Settings → Rain Bird → Test connection** (before connecting, or **Test** once connected) walks through it and says which step fails. It sends at most one request to the controller, and never while the hub is paused, so testing can't cause a lockout.

| What you see | Meaning | What to do |
|---|---|---|
| *Nothing answers at 192.168.x.x* | Wrong IP, the module is offline, or this Mac is on a different network (a guest network, for example) | Look for the module in your router's device list (often named RainBird or LNK), check its light, use the Mac on the same Wi-Fi |
| *Rejected the password* | The controller wants the **local** password set for the Wi-Fi module in the Rain Bird app, not your Rain Bird account password | Reconnect with the right one. The hub stops polling after one rejection |
| *Locked itself after repeated wrong passwords* | The controller blocks further attempts (HTTP 430) | Wait 15 minutes, or unplug it for a few seconds. The hub pauses its own polling for 16 minutes so it can't extend the lockout |
| *Busy with another request* | The controller handles **one request at a time** (the Rain Bird app counts) | Close the Rain Bird app and retry. The hub now sends its own requests one at a time |

A controller without a Wi-Fi module (LNK or LNK2) or built-in Wi-Fi has no network address at all and can't be reached by any app other than at the panel. Older controllers that don't answer the combined state request are read with simpler requests (no time-remaining).

## Hub schedules

Kasa and Hue schedules are saved on the plug or bridge and run even when the Mac is off. Every other device with an on/off (the Pentair pump, Govee, Dyson, Sonos, the LG TV, Ecovacs) can now be scheduled too, at set times. Those schedules run **in the hub**, so the Mac must be awake: System Settings → Energy → turn on "Prevent automatic sleeping when the display is off". The Schedules tab labels them **Hub (this Mac)**. Locks can't be scheduled.

## Hooking up the accounts for water, electricity and sprinkler analytics

Everything below is entered in the hub's **Settings** on your own Mac (from the iPad or iPhone over Tailscale). Passwords and keys stay in `~/.smarthome/secrets.json` on the Mac; never paste them into a chat.

1. **TXU electricity → Smart Meter Texas.** TXU has no API, but your meter reports to Smart Meter Texas, the state's meter portal.
   - Go to **smartmetertexas.com → Register → Residential customer**.
   - You need three things from a TXU bill: the **ESI ID** (17–22 digits), the **meter number**, and your retail provider (**TXU Energy**).
   - Then in the hub: **Settings → Smart Meter Texas**, with the Smart Meter Texas username and password (not your TXU login).
2. **McKinney water → My Water Advisor 2.**
   - Go to **mywateradvisor2.com** (or the "My Water Advisor 2.0" app) → **Join My Water Advisor** → sign up with your account number: all the digits **before the dash** on the water bill, and the account holder's name exactly as billed.
   - Then in the hub: **Settings → McKinney water**, with that email and password.
3. **Rain Bird.** **Settings → Rain Bird** with the controller's IP address (Rain Bird app → controller → Wi‑Fi, or your router's device list) and the **controller password** set in the Rain Bird app.
4. **Washer, dryer, AC → LG ThinQ.** At **connect-pat.lgthinq.com**, sign in with your LG account → **Add new token** → tick every permission → paste it in **Settings → LG ThinQ appliances**, country **US**.
5. **Pool pump → Pentair.** **Settings → Pentair** with the Pentair Home email and password.
6. **Weather.** **Insights → Set your location**: enter the city, e.g. "McKinney, TX". City level is enough; the street address isn't needed and isn't sent anywhere.
7. **Fetch history.** **Insights → Fetch last 4 weeks**. This pulls 28 days of hourly water, 15-minute electricity and weather, and replays your Rain Bird programs for those weeks (a replayed run is kept only if the water meter shows water in that hour, so rain-delay days drop out). It takes about a minute, mostly Smart Meter Texas. After that the hub tops up every 3 hours.

What can't be backfilled: **device activity** (when the washer, dryer, pool pump, AC… ran). LG, Pentair and the others don't keep a history the hub can read, so the hub records it every 2 minutes from the moment it runs this version. Per-device power needs about 6 runs that line up with the meter; until then those devices show "learning". The water/sprinkler and HVAC/temperature analyses work on the backfilled weeks right away.

## Electricity insights (Insights → Electricity)

- **Per-device power (kW):** each 15-minute meter slot where tracked devices ran gives one equation: power above the nearby idle level (±2 hours, so the AC's slow drift is taken out) = Σ (share of the slot each device ran × its kW). A least-squares fit over all those slots separates the dryer, pool pump, AC units, plugs and so on.
- **Heating and cooling:** daily kWh not explained by tracked devices is fitted to base + a × cooling degree-days + b × heating degree-days (65 °F balance point), with R² shown. The Lennox thermostat isn't connected yet; connecting it would replace the estimate with real run times.
- **Always-on load:** the lowest 15-minute power between 1 and 5 AM.
- **Spikes:** 15-minute slots far above normal (median + 6 × MAD, at least 3 kW), merged into events and labelled with whatever was running. Spikes with nothing tracked running are usually the AC or furnace starting, an oven or a water heater.

## Watering insights (Insights tab)

The hub keeps three histories in `~/.smarthome/hub.db` and lines them up by day and hour:

- **Sprinkler runs:** the hub checks the Rain Bird controller every 30 seconds and logs every zone run (zone, start, minutes), whether the controller's own programs, the hub, or the Rain Bird app started it. History starts when the hub starts watching.
- **Water use:** daily and hourly gallons from My Water Advisor, collected every 3 hours.
- **Weather:** daily rain, evaporation (FAO‑56 reference ET) and the high for your location, from [Open‑Meteo](https://open-meteo.com) (free, no account). Set the location in Insights ("McKinney, TX").

From those it works out:

- **Gallons per minute for each zone.** Each hour with watering gives one equation: water above that hour's normal household use = Σ (zone minutes × zone flow). A least-squares fit over those hours separates zones that run back to back. A zone needs at least 3 such hours, and zones that always run together in the same proportions aren't guessed. Needs the water meter's **hourly** data.
- **Daily split** of water into sprinklers vs everything else.
- **What the lawn needed:** last 7 days of evaporation × 0.7 (warm-season turf) − 80% of rain, compared with what each zone got. Enter each zone's area (sq ft) in the Sprinklers screen to get inch-based advice ("Zone 4: overwatered, try 32 min a week instead of 60").
- **Rain skips** from recent and forecast rain, and **leak warnings** when water keeps flowing between 1 and 5 AM with no watering.

### Data check: why the water-to-sprinkler link is or isn't captured

Insights → Water ends with a **Data check** card (and moves it to the top when something is missing). It walks the chain and says which link is broken and what to do:

1. **Sprinkler runs recorded:** the hub logs a run when it sees it start (it polls the controller every 30 seconds), and replays the controller's programs for past weeks once water data exists.
2. **Hourly water readings:** My Water Advisor is asked for hourly data one day at a time, as its own website does (a long date range may be refused). The check shows how many days returned data and the last error.
3. **Watering hours with a meter reading:** how many hours have both sprinkler minutes and a reading.
4. **Meter clock matches the sprinklers:** if the meter stamps an hour by its end, or in unlabelled UTC, the hub finds the offset (−12 to +12 hours) that makes water use line up with sprinkler minutes, corrects for it, and tells you. Needs about 6 watering hours and 2 days of hourly data.
5. **Gallons per minute by zone:** a zone is only estimated if its hour of the day has at least 3 watering-free days to compare against (a program that waters at the same hour every day leaves none) and its minutes vary enough to tell zones apart. Otherwise it shows "learning", and the **flow tests** measure it directly.
6. **Weather history.**

Hardening from an independent review (all tested, none seen on real data yet):
- **Units:** readings are converted to gallons from the meter's own unit (gallons, cubic feet, CCF/HCF, kgal, m³, litres). An unknown unit is flagged in the Data check instead of being assumed.
- **Odd timestamps:** ISO (with or without Z/offset), US dates (`11/20/2026 5:00 AM`), epoch seconds or milliseconds, and alternative field names are read; readings that can't be read are counted and the first one is shown.
- **Date format:** if the server refuses or returns nothing for ISO dates, the hourly request is retried with US dates (the daily endpoint uses them) and the working format is remembered.
- **Running totals:** if the readings are a meter total that only goes up, they are converted to use per hour.
- **Clock changes:** a meter that stamps UTC with no label is converted with daylight-saving rules, so it keeps matching across the November and March changes.
- **Pool fills and leaks:** the worst-fitting hour is dropped and the fit repeated (at most a quarter of the hours), and an implausible fit (over 40 gal/min) is discarded.
- **Missed runs:** the controller's programs are replayed for every day the hub saw nothing (Mac asleep, hub restarting), not only before it started watching, and kept only where the meter shows water. Cyclic ("every N days") programs can't be replayed.
- **Older controllers:** if a controller doesn't answer the combined state request, runs are read from the zone and rain-delay requests, and a failure is shown in the Data check instead of being logged quietly.
- **Flow tests** ignore an hour with no extra water (a placeholder reading, or a clock mismatch) and keep waiting.

### Rain Bird programs

- **Any controller:** run one of its programs now, from the Sprinklers screen.
- **ESP‑RZXe and ST8** (controllers that schedule each zone separately): edit each zone's minutes, start times (up to 6, in 10-minute steps) and days. The hub writes it to the controller and reads it back to confirm, so it runs even when the Mac is off.
- **ESP‑TM2, ESP‑ME3 and other program-based (A/B/C) controllers:** no library can write their programs, so use **smart programs** instead. They're run by the hub: start times, days and minutes per zone (set for peak summer). At each start the hub skips after 0.5 in of rain in two days or 0.25 in forecast for the day, otherwise scales every zone by last week's evaporation vs peak summer (30–120%). Every decision is logged with its reason. Turn the controller's own programs off in the Rain Bird app if you use these, and keep the Mac awake.

## Atmos Energy

Atmos has no API and no maintained library; Opower doesn't cover it. Options, from easiest:
1. **Bill emails:** Atmos e-bills arrive by email with the usage (Ccf) and amount. The hub could read them from Gmail (needs a Google sign-in for Gmail).
2. **Account Center scraping:** sign in to atmosenergy.com and read the usage history page. Fragile, and needs someone to look at the logged-in site once.
3. **Meter radio:** many Atmos meters broadcast readings that a $30 USB radio (rtl-sdr with rtlamr) can pick up. This gives near-real-time gas use with no account at all.

## Roadmap

1. ~~Core hub, Kasa and Hue, native schedules~~ (done)
2. ~~**Sign-in:** Google sign-in plus Face ID / Touch ID (passkeys), over HTTPS through Tailscale~~ (done)
3. **More devices:** ~~LG TV, Sonos, Govee (LAN), Ring, Nest thermostat, Lennox iComfort~~ (done). Next: Leviton.
4. **Siri:** a "schedule" Shortcut that turns spoken requests into hub schedules, then a HomeKit bridge for direct Siri control.
5. **Alexa and Google:** private skill and smart-home project, so both show the hub's names and rooms.
6. **Govee private API** investigation for timers.

## Device scanner

`scanner/smart_scan.py` finds smart devices on your local network using mDNS/Bonjour (Matter, HomeKit, Hue, Google Cast, Echo, AirPlay, Sonos…), SSDP/UPnP (LG and Sony TVs, Roku, Wemo…), and the Kasa and Govee local protocols. It only reads; it changes nothing on any device.

```bash
# On a Mac/PC connected to your home Wi-Fi:
python3 -m venv .venv && . .venv/bin/activate
pip install zeroconf
python3 scanner/smart_scan.py   # run twice; some devices answer slowly
```

Also check your router's "connected devices" list. Cloud-only gadgets (common with Smart Life) often don't answer local discovery.
