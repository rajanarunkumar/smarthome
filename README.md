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

`hub/` is a Python server (FastAPI) with a web app for the iPhone. It currently covers **Kasa** and **Hue**:

- **Rooms tab:** every device grouped by room, power switches, "all on/off" per room. Tap a device to rename it (the name is written back to the brand), move it to a room, or set brightness, color and white temperature.
- **Scenes tab:** your Hue scenes by room, including dynamic scenes.
- **Schedules tab:** one list of every schedule, whether made here or in the brand's app, labelled with where it runs. New schedules are saved on the plug or bridge itself.
- **Settings tab:** Hue Bridge pairing and room management.

### Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m hub            # prints a link with the access token; open it once on each device
```

On the iPhone (same Wi-Fi), open the printed `http://<mac-name>.local:8420/#token=…` link in Safari, then **Share → Add to Home Screen**. To pair Hue, press the round button on the Hue Bridge, then tap **Settings → Pair** within 30 seconds.

Private data lives in `~/.smarthome/` (the access token, Hue key and the rooms database), never in this repo.

Tests: `pip install -r requirements-dev.txt && python -m pytest tests`.

## Roadmap

1. ~~Core hub, Kasa and Hue, native schedules~~ (done)
2. **Sign-in:** Google sign-in plus Face ID / Touch ID (passkeys), over HTTPS through Tailscale. Replaces the access-token link.
3. **More devices:** Leviton and Lennox (HomeKit), Leviton Matter and Nest Thermostat (Matter), LG TV, Sonos, Govee, Ring.
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
