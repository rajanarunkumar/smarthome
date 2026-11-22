# Next steps

Read `.claude/HANDOVER.md` first (and `docs/HANDOVER.md` from the Mac session).

## Immediate
1. **User tries Dyson on the Mac:** Settings → Dyson → *Add from sticker* (Wi‑Fi name starting `DYSON-` and the Wi‑Fi password from the sticker on the purifier). Expected: the purifier appears in Rooms within a minute. If not: hub log `~/.smarthome/logs/hub.log`; check the product code is supported (`libdyson.get_device`) and that the Mac and purifier are on the same network (mDNS).
2. **Pull and restart the hub on the Mac** (`launchctl kickstart -k gui/$(id -u)/local.smarthome.hub`), after `pip install -r requirements.txt` in the service's venv (Python 3.13: Ecovacs, Rain Bird need it).
3. **Link the accounts** in Settings and run **Insights → Fetch last 4 weeks**. Collect the exact error text for anything that fails: My Water Advisor and Smart Meter Texas are the most likely to need fixes (response shapes were inferred, not seen).
4. Ask the user for the **Rain Bird controller model** (decides zone schedules vs smart programs).
5. Take a first photo of each zone and run **Test all zones tonight** (flow tests) so gal/min is measured.

## Then
- Lennox thermostat, Leviton, Atmos (see HANDOVER).
- Native iOS app under `ios/` (Mac session's plan; needs Xcode).
- Port Sonos alarms/favorites from branch `sonos-native-alarms`.
