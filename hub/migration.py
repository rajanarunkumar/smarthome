"""Settings → Move to Home Assistant: one row per brand, so it's always clear where each connection lives.

For each brand: how many devices the hub has, how many of them HA has (matched by name), what HA will ask for,
anything the hub can show that isn't secret (an IP address, a project ID), the steps, the source switch (hub or
Home Assistant) and, when done, removing the hub's stored login. Brands with no HA replacement say so.
"""

from __future__ import annotations

from typing import Any

from .adapters.ha_adapter import BRAND_PLATFORMS, norm

# brand -> (what HA asks for, steps). Order = the order to move them in.
GUIDE: dict[str, tuple[str, str]] = {
    "hue": ("Press the Hue Bridge button.", "Appears under Settings → Devices & services → Discovered. Add it, press the bridge button."),
    "kasa": ("Nothing, or the Kasa app email and password for newer plugs.", "Appears as TP-Link Smart Home under Discovered. Add each."),
    "sonos": ("Nothing.", "Appears under Discovered. Add it."),
    "lg": ("Accept the prompt on the TV.", "Turn the TV on; LG webOS TV appears under Discovered. Add it and accept on the TV with the remote."),
    "govee": ("LAN Control on in the Govee app; for cloud-only Govee devices, your Govee API key.",
              "Govee Lights Local appears under Discovered. For plugs and other cloud-only devices, install the Govee integration from HACS and paste the API key."),
    "ring": ("Ring email and password, then the code Ring sends.", "Add integration → Ring."),
    "schlage": ("Schlage email and password (the second account, if you made one).", "Add integration → Schlage."),
    "ecovacs": ("Ecovacs email, password and country.", "Add integration → Ecovacs."),
    "thinq": ("A personal access token from connect-pat.lgthinq.com and your country.", "Make a new token (keep the hub's until you switch). Add integration → LG ThinQ."),
    "tuya": ("Your Smart Life User Code (Smart Life app → Me → ⚙ → Account and Security → User Code), then scan HA's QR code with the Smart Life app.",
             "Add integration → Tuya. No developer project needed; delete the hub's Tuya project afterwards."),
    "rainbird": ("The controller's IP address and controller password (the same as the hub's).",
                 "Add integration → Rain Bird. Move this one last: the hub still logs sprinkler runs from the controller until HA's events replace that."),
    "nest": ("The Device Access project ID, the OAuth client ID and secret, and a Google Cloud Pub/Sub topic (HA's setup walks you through it).",
             "Add integration → Google Nest. Reuse the same Device Access and Cloud project; add HA's redirect address to the OAuth client when HA shows it."),
    "lennox": ("The iComfort email and password, or the thermostat's IP.", "Install Lennox S30 from HACS, then add it."),
    "dyson": ("The MyDyson sign-in, or each purifier's serial, credential and type (from the Wi‑Fi sticker, as today).",
              "Install Dyson Local from HACS, then add each purifier."),
    "pentair": ("No confirmed Home Assistant integration for Pentair Home yet.", "Stays in the hub for now."),
}
STAYS = {"smt": "Smart Meter Texas: HA only reads the meter total; the hub needs the 15-minute data.",
         "water": "My Water Advisor: no Home Assistant integration.",
         "pentair": "Pentair Home: no confirmed Home Assistant integration yet."}


def hints(hub: Any, brand: str) -> list[str]:
    """Things HA will ask for that aren't secret and the hub already knows."""
    s = hub.secrets
    out = []
    if brand == "rainbird" and (s.get("rainbird") or {}).get("host"):
        out.append(f"Controller IP: {s.get('rainbird')['host']}")
    if brand == "lennox" and (s.get("lennox") or {}).get("ip"):
        out.append(f"Thermostat IP: {s.get('lennox')['ip']}")
    if brand == "nest" and (s.get("nest") or {}).get("project_id"):
        out.append(f"Device Access project ID: {s.get('nest')['project_id']}")
    if brand == "thinq" and (s.get("thinq") or {}).get("country"):
        out.append(f"Country: {s.get('thinq')['country']}")
    if brand == "dyson":
        for d in (s.get("dyson") or {}).get("devices") or []:
            out.append(f"{d.get('name')}: serial {d.get('serial')}, type {d.get('device_type')}")
    return out


SECRET_KEY = {"lg": "lgtv", "govee": "govee_api_key", "hue": "hue", "kasa": None, "sonos": None}


def report(hub: Any) -> dict:
    ha = hub.ha
    state = hub.secrets.get("ha_migration") or {}
    rows = []
    for brand, (needs, steps) in GUIDE.items():
        adapter = next((a for a in hub.adapters if a.brand == brand), None)
        if adapter is None:
            continue
        hub_devices = [d for d in hub.last_devices.values() if d.brand == brand and not d.id.startswith("ha:")] or \
            list(hub.device_cache.get_if_present(brand) or [])
        ents = ha.entities_for(brand) if ha.connected else []
        ha_names = {norm(ha.name(e)) for e in ents}
        matched = [d.name for d in hub_devices if norm(d.name) in ha_names]
        unmatched = [d.name for d in hub_devices if norm(d.name) not in ha_names]
        st = state.get(brand) or {}
        key = SECRET_KEY.get(brand, brand)
        rows.append({
            "brand": brand, "label": adapter.label, "stays": brand in STAYS, "note": STAYS.get(brand),
            "hub_devices": len(hub_devices), "hub_connected": bool(adapter.active) or bool(key and hub.secrets.get(key)),
            "ha_entities": len(ents), "matched": len(matched), "unmatched": unmatched[:12],
            "needs": needs, "steps": steps, "hints": hints(hub, brand),
            "source": st.get("source", "hub"), "moved": bool(st.get("moved")),
            "platforms": list(BRAND_PLATFORMS.get(brand, ())),
        })
    for brand, note in STAYS.items():
        if brand not in GUIDE:
            adapter = next((a for a in hub.adapters if a.brand == brand), None)
            rows.append({"brand": brand, "label": adapter.label if adapter else brand, "stays": True, "note": note,
                         "hub_devices": None, "hub_connected": bool(adapter and adapter.active), "source": "hub", "moved": False})
    done = all(r["stays"] or r["moved"] or not r["hub_connected"] for r in rows)
    return {"ha": {"connected": ha.connected, "version": ha.version, "entities": len(ha.states), "error": ha.error,
                   "configured": ha.active}, "rows": rows, "done": done}


def set_source(hub: Any, brand: str, source: str) -> None:
    state = dict(hub.secrets.get("ha_migration") or {})
    entry = dict(state.get(brand) or {})
    entry["source"] = source
    state[brand] = entry
    hub.secrets.set("ha_migration", state)
    hub.apply_migration()


async def remove_from_hub(hub: Any, brand: str) -> None:
    """The brand now lives in HA: delete the hub's stored login for it and mark it moved."""
    adapter = next((a for a in hub.adapters if a.brand == brand), None)
    if adapter is not None and hasattr(adapter, "disconnect"):
        await adapter.disconnect()
    key = SECRET_KEY.get(brand, brand)
    if key:
        hub.secrets.set(key, None)
    state = dict(hub.secrets.get("ha_migration") or {})
    state[brand] = {**(state.get(brand) or {}), "source": "ha", "moved": True}
    hub.secrets.set("ha_migration", state)
    hub.apply_migration()
