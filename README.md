# smarthome

The goal is to control every smart device from **one iPhone app**, with all of them still working in **Google Home and Alexa**.

## Recommendation: Home Assistant as the hub, plus Matter where you can

```
                 ┌──────────── iPhone: Home Assistant app (one app for everything)
                 │
  Hue ─┐         │            ┌──► Alexa        (voice, Echo devices)
  Govee ┤        ▼            ├──► Google Home  (voice, Nest devices)
  Kasa ─┼──► Home Assistant ──┤
  Leviton┤   (small box at    └──► Apple Home   (optional, via HomeKit Bridge → Siri)
  Smart Life┤ home, local)
  LG ───┤
  Sony ─┘
```

**Why not just Google Home, Alexa or Apple Home?** Each of them misses some of your devices or controls them only through slow cloud "skills". Home Assistant has an integration for every brand you listed, runs most of them **locally** (fast, and still works when the internet is down), and then **publishes one clean device list** to Alexa and Google Home. You rename or group a device once and every app sees the change.

**Why not write a custom iPhone app?** Hue, Govee, Kasa, Leviton, Tuya, LG and Sony each have different APIs and logins, and the APIs change. Home Assistant already maintains all of these integrations and includes a polished iOS app with widgets, Apple Watch support, Siri Shortcuts and location-based automations. If you want a custom look later, a dashboard can be built *on top of* Home Assistant.

## Your devices

| Brand | Best way into Home Assistant | Local? | Matter option | Notes |
|---|---|---|---|---|
| Philips **Hue** | Hue integration (auto-discovered bridge) | ✅ | Bridge v2 supports Matter | Keep the bridge. It is the most reliable option for Hue. |
| **Govee** | Govee Lights Local (enable *LAN Control* in the Govee app); Matter for newer models | ✅ (LAN models) | Newer models | Older models without LAN support need the cloud API key integration. |
| TP-Link **Kasa** | TP-Link Smart Home integration | ✅ | KP125M / EP25-class plugs | Kasa accounts are being merged into the Tapo app. The HA integration covers both. |
| **Leviton** Decora Smart | 2nd-gen Wi-Fi models (D215S switch, D26HD dimmer): **HomeKit Controller**, local. Matter models: Matter integration | ✅ | Yes (Matter line) | The scan found both kinds. The HomeKit ones are unpaired, so HA can adopt them directly with the 8-digit code on the device. The Matter ones are already in Alexa; share them to HA with Alexa's "pair with another app" (multi-admin) without resetting them. |
| **Smart Life** (Tuya) | Tuya integration (cloud); optional *LocalTuya* or *Tuya Local* (HACS) for local control | ☁️ / ✅ | Rare | This is usually the weakest link, so replace it with Matter devices over time. |
| **LG** (ThinQ appliances, webOS TV) | LG ThinQ integration plus LG webOS TV integration | TV ✅ / appliances ☁️ | Some newer ThinQ | The TV can also be added to Apple Home via AirPlay/HomeKit. No ThinQ appliances answered the scan; they are cloud-only. |
| **Sony** (Bravia TV, speakers) | Sony Bravia TV integration (enable *Remote start* and set a PSK on the TV); Google Cast | ✅ | – | Google TV models also appear in Google Home natively. Not seen in the scan; it was probably off. |
| **Lennox** iComfort E30 thermostat *(found by scan)* | **HomeKit Controller** (the E30 advertises HomeKit and is unpaired), or the *Lennox S30/E30* HACS integration for more sensors and modes | ✅ | – | Both options are local. Use the HomeKit pairing code shown in the thermostat's settings. |
| **Sonos** (Beam, Amp) *(found by scan)* | Sonos integration (auto-discovered) | ✅ | – | Also does AirPlay 2 and Spotify Connect. HA can use them for announcements. |
| **Amazon Echo** *(found by scan)* | Not needed as a device. Optional *Alexa Media Player* (HACS) for announcements | ☁️ | Matter controller; two Echos are Thread border routers | Stays as the Alexa voice front end. Your Leviton Matter switches are currently paired to Alexa. |
| **Google Nest** (Hubs, Home, Mini) *(found by scan)* | Google Cast integration (media, TTS announcements) | ✅ | Matter controller | Stays as the Google voice front end and displays. |
| **Apple TV** 4K *(found by scan)* | Apple TV integration | ✅ | Matter/HomeKit home hub | Needed only if you use Apple Home or Siri via HomeKit Bridge. |

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

## Setup steps

1. **Run the scanner** (below) to find every device on your Wi-Fi, including brands not in this list.
2. **Get a hub.** A Home Assistant Green is the simplest option. A Raspberry Pi 5 or a spare mini PC also works. Add a Thread/Matter radio (for example a Home Assistant Connect ZBT‑1/ZBT‑2) if you plan to buy Matter-over-Thread devices.
3. **Install the Home Assistant iPhone app** (App Store: *Home Assistant*), then finish onboarding. Most devices on the network are auto-discovered.
4. **Add each brand** using the table above. Prefer local integrations where possible.
5. **Connect to Alexa and Google Home.**
   - *Easy:* a Home Assistant Cloud (Nabu Casa) subscription. It links Alexa and Google in a few taps and gives secure remote access to the iPhone app.
   - *Free:* the manual Alexa Smart Home skill and Google Assistant setup using your own domain and certificates. This takes more work.
   - In the Alexa and Google apps, **unlink the individual brand skills** (Hue, Govee, Kasa…) after Home Assistant is connected. Otherwise every device appears twice.
6. *(Optional)* Turn on **HomeKit Bridge** in Home Assistant to get Siri, Apple Home and Control Center tiles too.
7. **Going forward:** buy **Matter** devices. They pair into Home Assistant, Alexa, Google and Apple at the same time (multi-admin), with no brand app or account needed.

## Device scanner

`scanner/smart_scan.py` finds smart devices on your local network using mDNS/Bonjour (Matter, HomeKit, Hue, Google Cast, Echo, AirPlay, Sonos…), SSDP/UPnP (LG and Sony TVs, Roku, Wemo…), and the Kasa and Govee local protocols. It only reads; it changes nothing on any device.

```bash
# On a Mac/PC connected to your home Wi-Fi:
python3 -m venv .venv && . .venv/bin/activate
pip install zeroconf
python3 scanner/smart_scan.py   # run twice; some devices answer slowly
```

Also check your router's "connected devices" list. Cloud-only gadgets (common with Smart Life) often don't answer local discovery.
