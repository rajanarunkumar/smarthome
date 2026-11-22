# Next steps

Read `.claude/HANDOVER.md` first (and `docs/HANDOVER.md` from the Mac session).

## Immediate
0. **Rain Bird (user: password works in the Rain Bird app, hub won't connect).** Ask the user to pull, `pip install -U -r requirements.txt` in the hub venv (pyrainbird must be >= 6.1 for HTTPS-only LNK2 firmware), restart, then run `.venv/bin/python -m hub.adapters.rainbird_adapter --find` and paste the output plus the controller model. Likely causes: IP changed, controller on an IoT/guest network (the app works through Rain Bird's cloud), old pyrainbird. The Mac was never attached to the cloud session, so this is untested against the real controller.
0. **Ring and Nest are built, untested against real accounts.** Ring: Settings → Ring (email, password, code). Nest: README "Nest thermostat" (US$5 Device Access, SDM API, OAuth web client with the hub's `https://…ts.net/` redirect, app In production). Lennox: waiting on the user (local IP vs iComfort login).
1. **User tries Dyson on the Mac:** Settings → Dyson → *Add from sticker* (Wi‑Fi name starting `DYSON-` and the Wi‑Fi password from the sticker on the purifier). Expected: the purifier appears in Rooms within a minute. If not: hub log `~/.smarthome/logs/hub.log`; check the product code is supported (`libdyson.get_device`) and that the Mac and purifier are on the same network (mDNS).
2. **Pull and restart the hub on the Mac** (`launchctl kickstart -k gui/$(id -u)/local.smarthome.hub`), after `pip install -r requirements.txt` in the service's venv (Python 3.13: Ecovacs, Rain Bird need it).
3. **Link the accounts** in Settings and run **Insights → Fetch last 4 weeks**. Collect the exact error text for anything that fails: My Water Advisor and Smart Meter Texas are the most likely to need fixes (response shapes were inferred, not seen).
4. Ask the user for the **Rain Bird controller model** (decides zone schedules vs smart programs).
5. Take a first photo of each zone and run **Test all zones tonight** (flow tests) so gal/min is measured.

## Then
- Lennox thermostat, Leviton, Atmos (see HANDOVER).
- Native iOS app under `ios/` (Mac session's plan; needs Xcode).
- Port Sonos alarms/favorites from branch `sonos-native-alarms`.
