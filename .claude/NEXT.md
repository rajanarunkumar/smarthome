# Next: Dyson sign-in for Google-linked accounts

The user's Dyson account is created through Google sign-in, so there is **no Dyson password**. The current flow (`hub/adapters/dyson_adapter.py`: `send_code` then `verify_code(code, password)`) requires one.

## What to find out first (don't guess)
- Does `libdyson-neon`'s `DysonAccount.login_email_otp` accept an empty/arbitrary password for an account with none? Read `libdyson/cloud/account.py` `_verify(otp_code, password)`: the password is sent to Dyson's `/v3/userregistration/email/verify` as part of the body. Social-login accounts may not have one set.
- Dyson's own mobile app lets a user set or reset a password from "Forgot password" even on a social-login account. That is the simplest route for the user: reset a password for the same email in the MyDyson app, then use email + code + that new password in the hub.
- Alternative needing no password at all: **local credentials**. Dyson devices print a Wi-Fi SSID and password on a label, and `libdyson` has `get_mqtt_info_from_wifi_info(ssid_password, device_type)` which derives the local MQTT password directly from the sticker, so no Dyson account is needed. The hub then connects over the LAN by mDNS. Needs the device's serial (from the same sticker) and its product type code.

## Plan
1. Check which of the two paths works with libdyson-neon (read the source in the installed package, `libdyson.get_mqtt_info_from_wifi_info`).
2. Preferred UI: in Settings → Dyson, offer "Use the sticker on the device" (serial + Wi-Fi password from the label) alongside the email/OTP path, with a plain explanation that Google-linked accounts should use the sticker.
3. Add tests with fakes, update README Dyson row and this file, commit.

Status: not started (handover written first, per the user's request to commit before the rate limit).
