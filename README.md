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
| **Leviton** Decora Smart | Matter (newer Decora Smart Matter line); older Wi-Fi models via cloud | ✅ with Matter | Yes (Matter line) | Check the model number. Matter models pair directly into HA. |
| **Smart Life** (Tuya) | Tuya integration (cloud); optional *LocalTuya* or *Tuya Local* (HACS) for local control | ☁️ / ✅ | Rare | This is usually the weakest link, so replace it with Matter devices over time. |
| **LG** (ThinQ appliances, webOS TV) | LG ThinQ integration plus LG webOS TV integration | TV ✅ / appliances ☁️ | Some newer ThinQ | The TV can also be added to Apple Home via AirPlay/HomeKit. |
| **Sony** (Bravia TV, speakers) | Sony Bravia TV integration (enable *Remote start* and set a PSK on the TV); Google Cast | ✅ | – | Google TV models also appear in Google Home natively. |

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
pip3 install zeroconf
python3 scanner/smart_scan.py
```

Also check your router's "connected devices" list. Cloud-only gadgets (common with Smart Life) often don't answer local discovery.
