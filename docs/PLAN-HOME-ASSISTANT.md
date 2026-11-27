# Plan: Home Assistant runs the devices, the hub keeps the insights

Status: decided 2026-11-26, being built. **Built so far:** the hub's Home Assistant connection
(`hub/adapters/ha_adapter.py`: WebSocket, registries, live state changes, service calls) and the checklist
(`hub/migration.py`, Settings → Home Assistant → Move to Home Assistant…). **Next:** Rain Bird run logging from HA
events, matching history across the id change (`kasa:…` → `ha:light.…`), then deleting moved adapters. Decisions:
1. **HA runs in a virtual machine on the Mac** (setup below).
2. **Alexa and Google: the free manual setup** (what it needs is below).
3. **Combo:** the HA app and HA Assist for controlling devices; the hub for insights, Ask Home's reviews and
   questions, and the migration checklist. The hub's Rooms, Scenes and Schedules tabs go once every brand has moved.

## The idea

Home Assistant (HA) becomes the device layer: every brand connection, rooms (HA "areas"), names, scenes, schedules
(HA automations), voice (Siri, Alexa, Google) and the phone app for controlling things. Thousands of people maintain
those integrations; we stop maintaining ours.

The hub becomes the **insights app**: water and sprinkler correlation, zone flow and photos, smart irrigation,
electricity breakdown against the TXU free-nights plan and the bills, HVAC run time and cost, pool pump cost, always-on
load, Ask Home's reviews, Admin. It reads devices from HA instead of from each brand, and it hears every change the
moment it happens (HA pushes events), so the polling that cost CPU goes away.

```
 iPhone: HA app (control, voice)          iPhone/iPad: hub (Insights, Ask Home) — also as an HA sidebar panel
              │                                         │
        Home Assistant  ──── WebSocket: states + events ───▶  hub (insights, history, bills, water)
   (all brands, areas, automations,       ◀── service calls (open blinds, run zone) ──
    scenes, Siri/Alexa/Google)                          │
                                                Smart Meter Texas 15-min, My Water Advisor, Open-Meteo, Claude/Gemini
```

## What moves to HA, and what code goes

| Hub today | In HA | Hub code removed |
|---|---|---|
| Hue | core Philips Hue | `hue_adapter.py`, `color.py` |
| Kasa | core TP-Link Smart Home (incl. energy-monitoring plugs) | `kasa_adapter.py` |
| Govee | core Govee Lights Local (LAN); Govee cloud via HACS | `govee_*.py` (3 files) |
| Sonos | core Sonos | `sonos_adapter.py` |
| LG TV | core LG webOS TV | `lgtv_adapter.py` |
| LG ThinQ washer/dryer | core LG ThinQ (same personal access token) | `thinq_adapter.py` |
| Dyson | HACS "Dyson Local" (same library, libdyson-neon) | `dyson_adapter.py` |
| Ecovacs | core Ecovacs | `ecovacs_adapter.py` |
| Schlage | core Schlage | `schlage_adapter.py` |
| Ring | core Ring | `ring_adapter.py` |
| Nest | core Google Nest (same Device Access project) | `nest_adapter.py` |
| Lennox E30 | HACS "Lennox S30" (same library, lennoxs30api) | `lennox_adapter.py` |
| Smart Life blinds | core Tuya: sign in by scanning a code in the Smart Life app, **no developer project or trial to renew** | `tuya_adapter.py` |
| Rain Bird | core Rain Bird (same library): zones as switches, rain delay | `rainbird_adapter.py` control and polling (the run log comes from HA events instead) |
| Pentair pump | core Pentair ScreenLogic only fits a ScreenLogic adapter; for the Pentair Home cloud, check HACS first | keep `pentair_adapter.py` until an HA integration is confirmed |
| Leviton, Matter devices, garage (ratgdo/ESPHome) | core Matter, ESPHome | never built, nothing to remove |
| Rooms, names | HA areas and names | hub rooms/names storage and UI (`store.py` parts, Rooms tab) |
| Scenes | HA scenes | Scenes tab, brand scene code |
| Schedules | HA automations (sun offsets, conditions, notifications) | `scheduler.py`, `schedule_codec.py`, Schedules tab |
| Device cache, warmer, activity polling | HA keeps state; the hub subscribes to changes | `cache.py` device parts, warm/record loops |
| Siri, Alexa, Google | HA HomeKit Bridge (free, Siri); Alexa/Google via Home Assistant Cloud (paid subscription) or a manual setup | the planned voice work |
| Device control from chat | HA Assist with an Anthropic, Gemini or OpenAI conversation agent | Ask Home's control tool (Ask Home keeps reviews and insight questions) |
| MCP server (`/mcp`) | HA has its own MCP server integration | `mcp_server.py`, `mcp_clients.py`, the .mcpb extension, if HA's is enough |
| Bulb power table (Powercalc) | HACS Powercalc, the source of that table, gives every bulb a power sensor | `lights.py`, `data/light_power.json` |

Roughly **5,000 lines of brand connectors and 1,000+ lines of schedules, scenes, rooms and cache code** go, with
their tests. That is about half the codebase, and nearly all the parts that break when a brand changes its cloud.

## What stays in the hub (nothing in HA does this)

- **Water:** My Water Advisor adapter, hourly water vs sprinkler runs, per-zone gallons per minute, flow tests, leak
  check, zone photos with Claude, smart irrigation programs tied to the meter and weather. (HA's Rain Bird integration
  runs zones; the hub decides how long.)
- **Electricity:** Smart Meter Texas 15-minute intervals (HA's integration reads only the meter total), the TXU
  free-nights tariff, bill import and reconciliation, always-on load from quiet weekday afternoons, fridges and
  freezers, device and HVAC cost, the 4-hourly collection and on-demand insights.
- **HVAC and pool insights:** run time, short cycling, cost by free and paid hours (fed by HA's thermostat and pump states).
- **Ask Home:** naming and schedule reviews and insight questions. Its plans would apply through HA (rename entities,
  set areas, create automations) instead of through each brand.
- **Admin, backups to S3, updater, profiler** (smaller, as there is less to watch).

## How the hub talks to HA

- One **long-lived access token** from HA (Profile → Security), saved in Settings like the other keys.
- **WebSocket API**: `get_states` once, then `subscribe_events: state_changed`. Each change updates the hub's copy
  and the activity log with the exact time (today the log is sampled every 2 or 10 minutes).
- **Service calls** for the few actions the hub itself takes: run a sprinkler zone, set rain delay, open blinds from
  an insight. Everything else is controlled in the HA app.
- Mapping HA domains to the hub's device kinds: `light`, `switch`, `cover` (blinds, garage), `climate` (thermostats),
  `lock`, `media_player`, `vacuum`, `camera`, `sensor` (power, energy, water).
- HA's **history API** fills gaps after a hub restart, so no activity is lost.

## Phases

Each phase leaves everything working; old adapters stay until HA is proven for that brand.

1. **Install HA.** Recommended: a **Home Assistant Green** (about $99, plug-and-play, always on) or HA OS in a
   virtual machine on the Mac. Docker on macOS can't see the network the way HA needs, so discovery
   (Hue, Sonos, Kasa, Matter) breaks; avoid it. Add the Tailscale add-on for remote access.
2. **Add every integration in HA** from the table, starting with the easy ones (Hue, Kasa, Sonos, LG TV, Ring,
   Schlage, Tuya by Smart Life QR, Rain Bird), then Nest, ThinQ, Lennox, Dyson. Check each from the HA app.
   Set up areas and names in HA (Ask Home's review can propose them).
3. **Hub reads from HA.** New `homeassistant` adapter (WebSocket client, mapping, service calls) and a per-brand switch
   "source: hub / Home Assistant", so each brand can move on its own and move back if needed. The insights code
   doesn't change; it receives the same device records.
4. **Move schedules and scenes to HA automations** (the hub lists its current ones for copying; Ask Home can draft them).
5. **Delete the duplicated code** brand by brand once each has run on HA for a week: adapters, tests, settings
   cards, scheduler, scene code, Rooms and Schedules tabs. The hub's app becomes Insights, Ask Home and Admin.
6. **Voice and one app:** HomeKit Bridge for Siri; Alexa/Google if wanted; add the hub to HA's sidebar
   (a panel showing the hub's page) so the HA app opens Insights too.

## Home Assistant in a virtual machine on the Mac

- **UTM** (free, utm.app) running **Home Assistant OS**: the *aarch64 / Generic ARM* image (`.qcow2`) for Apple
  silicon Macs, the *generic x86-64* image for Intel Macs, from home-assistant.io → Installation → macOS.
- VM settings: 2 CPUs, 4 GB memory, the image as the boot disk (32 GB is plenty), **network mode Bridged** (so HA
  is a device on the home network and can discover Hue, Sonos, Kasa, Matter; Shared/NAT mode breaks discovery).
- Start it at login: UTM has no autostart setting; a login item runs `/Applications/UTM.app/Contents/MacOS/utmctl start "Home Assistant"` (steps in the install guide). Keep the Mac from sleeping, as the hub already needs.
- HA is then at `http://homeassistant.local:8123` (or the address shown in the VM window). In HA: create the owner
  account, then Settings → Add-ons → **Tailscale** for remote access, and Profile → Security → **Long-lived access
  token** for the hub.
- Cost on the Mac: about 2 GB of memory and a few percent of one core when idle.

## Alexa and Google: the free manual setup

Both reach HA from the internet, so HA needs a public **HTTPS** address first: Tailscale **Funnel** on HA's Tailscale
add-on is the simplest (only the paths Alexa/Google call need to be open; the HA login protects the rest).
- **Alexa:** HA docs → *Amazon Alexa Smart Home Skill*: a private skill in the Alexa developer console plus a small
  AWS Lambda function (free tier) that forwards to HA. About an hour, once.
- **Google Home:** HA docs → *Google Assistant* (manual): a project in the Google Home developer console with
  account linking to HA, and `google_assistant:` in HA's configuration. About an hour, once.
- Siri needs none of this: HA's **HomeKit Bridge** works on the home network.
- Which devices each one sees is chosen in HA (*expose* per entity), so locks and blinds can stay off voice if wanted.

## Onboarding: moving each connection, step by step

Every brand is connected once in HA, checked, and only then disconnected from the hub. Nothing is moved by copying
secrets between the two: HA asks for its own sign-in, which keeps each side revocable on its own.

### In the hub: Settings → Move to Home Assistant

A checklist page, one row per brand, so it's always clear where each connection lives:

| Column | What it shows |
|---|---|
| Brand | with the number of devices the hub has for it today |
| In Home Assistant | *not yet* / *found: 5 of 6 devices* (matched to HA entities by name and model, unmatched ones listed) |
| You'll need | what HA will ask for, and anything the hub can show that isn't secret (Rain Bird IP, Lennox thermostat IP, Nest project ID, country) |
| Steps | the steps below for that brand, with links to the HA page |
| Source | *hub* / *Home Assistant*, switchable both ways for a week |
| Finish | **Remove from hub**: deletes that brand's stored login from `secrets.json`, then the row shows ✓ *moved* |

Passwords and tokens are never displayed; where HA wants the same one (Rain Bird controller password), the page says
which one and where it was set. Brands that stay in the hub (My Water Advisor, Smart Meter Texas, Pentair until
confirmed) are listed as *stays in the hub*, so nothing is left ambiguous. First row: **Home Assistant itself**
(address + long-lived access token, with a test button).

### What each brand needs in HA

In HA: **Settings → Devices & services → Add integration**, unless it appears there by itself (*Discovered*).

| Brand | How it connects in HA | What you need | Reused from today |
|---|---|---|---|
| Hue | discovered | press the bridge button | — |
| Kasa | discovered | newer plugs ask for the TP-Link (Kasa app) email and password once | — |
| Sonos | discovered | nothing | — |
| LG TV | discovered | accept the prompt on the TV | — |
| Govee | *Govee Lights Local* (discovered) | LAN Control on in the Govee app, as today | — |
| Govee cloud-only devices | HACS Govee integration | the Govee API key | the key you already have (Govee app → Profile → Apply for API Key shows it again) |
| LG ThinQ | *LG ThinQ* | a personal access token and country | make a new token at connect-pat.lgthinq.com (keep the hub's working until the switch) |
| Dyson | HACS *Dyson Local* | either the MyDyson sign-in, or each device's serial, credential and type | for Google-sign-in accounts the hub shows each purifier's serial and type; the Wi‑Fi sticker gives the rest, as today |
| Ecovacs | *Ecovacs* | Ecovacs email, password, country | — |
| Schlage | *Schlage* | Schlage email and password | the second account the hub uses, if you made one |
| Ring | *Ring* | Ring email and password, then the code Ring sends | — |
| Nest | *Google Nest* | the Device Access project ID, an OAuth client ID and secret, and a Google Cloud Pub/Sub topic HA's setup walks you through | the same Device Access project and Cloud project; add HA's redirect address to the OAuth client (HA shows it) |
| Lennox E30 | HACS *Lennox S30* | iComfort email and password, or the thermostat's IP | same choice as today; the hub shows the IP if you used local |
| Smart Life blinds | *Tuya* | the Smart Life **User Code** (Smart Life app → Me → ⚙ → Account and Security → User Code), then scan HA's QR code with the Smart Life app | no developer project needed; the hub's Tuya project can be deleted afterwards |
| Rain Bird | *Rain Bird* | controller IP and controller password | the same IP and password as the hub (the hub shows the IP) |
| Pentair Home | (to be confirmed) | — | stays in the hub for now |
| My Water Advisor, Smart Meter Texas | — | — | stay in the hub |

### Order

1. Home Assistant itself (install, then the hub's first row).
2. The discovered ones: Hue, Kasa, Sonos, LG TV, Govee local. Nothing to type, quickest to check.
3. Sign-in brands: Ring, Schlage, Ecovacs, ThinQ, Tuya (blinds), Rain Bird.
4. The involved ones: Nest (Pub/Sub), Lennox and Dyson (HACS).
5. For each: check the devices in the HA app, switch the hub's *Source* to Home Assistant, live with it a week,
   then **Remove from hub**.

### Done means

- Every row reads ✓ *moved* or *stays in the hub*.
- `secrets.json` holds only: Home Assistant token, My Water Advisor, Smart Meter Texas, Pentair, the AI keys, S3 backup.
- The deleted adapters' code and tests are gone (phase 5).

## Decisions for you

1. **Where HA runs:** Home Assistant Green (recommended), a Raspberry Pi 5, or a VM on the Mac.
2. **Phone app for control:** HA's app (recommended, and the hub drops Rooms/Scenes/Schedules) or keep the hub's
   Rooms screen as well, reading from HA.
3. **Alexa and Google:** Home Assistant Cloud (paid subscription, a few clicks) or the free manual setup (more work).
4. **Chat:** HA Assist for spoken and typed control, Ask Home for reviews and insights (recommended), or Ask Home only.

## Risks

- Lennox and Dyson come from HACS (community add-ons), not HA core; same libraries the hub uses today, so no worse.
- Pentair Home may have no HA integration; its adapter stays until one is confirmed.
- Two systems to update instead of one; HA updates monthly and occasionally renames entities.
- Insights depend on HA being up; the hub shows the last known states and backfills from HA's history.
