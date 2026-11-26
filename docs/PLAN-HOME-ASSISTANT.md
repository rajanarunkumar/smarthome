# Plan: Home Assistant runs the devices, the hub keeps the insights

Status: proposal, nothing changed yet. Written 2026-11-26.

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
