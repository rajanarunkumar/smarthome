"""Govee LAN API messages (UDP JSON). Pure functions, so they can be tested offline.

Protocol: the hub multicasts "scan" to 239.255.255.250:4001; lights answer on
UDP 4002. Commands go to each light on UDP 4003; "devStatus" answers on 4002.
A light only answers after "LAN Control" is turned on for it in the Govee app.
"""

from __future__ import annotations

import json

SCAN_GROUP = ("239.255.255.250", 4001)
REPLY_PORT = 4002
COMMAND_PORT = 4003
KELVIN_MIN, KELVIN_MAX = 2000, 9000


def _msg(cmd: str, data: dict) -> bytes:
    return json.dumps({"msg": {"cmd": cmd, "data": data}}, separators=(",", ":")).encode()


def scan() -> bytes:
    return _msg("scan", {"account_topic": "reserve"})


def status_request() -> bytes:
    return _msg("devStatus", {})


def turn(on: bool) -> bytes:
    return _msg("turn", {"value": 1 if on else 0})


def brightness(percent: int) -> bytes:
    return _msg("brightness", {"value": max(1, min(100, int(percent)))})


def color(hex_color: str) -> bytes:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return _msg("colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})


def mirek_to_kelvin(mirek: int) -> int:
    return max(KELVIN_MIN, min(KELVIN_MAX, round(1_000_000 / mirek)))


def kelvin_to_mirek(kelvin: int) -> int:
    return round(1_000_000 / kelvin)


def color_temp(mirek: int) -> bytes:
    return _msg("colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": mirek_to_kelvin(mirek)})


def parse(payload: bytes) -> tuple[str, dict] | None:
    """Return (cmd, data) from a Govee reply, or None if it isn't one."""
    try:
        msg = json.loads(payload)["msg"]
        return str(msg["cmd"]), dict(msg.get("data") or {})
    except (ValueError, KeyError, TypeError):
        return None


def status_to_state(data: dict) -> dict:
    """Turn a devStatus reply into hub Device fields."""
    out: dict = {"on": bool(data.get("onOff")), "brightness": data.get("brightness") or None}
    kelvin = data.get("colorTemInKelvin") or 0
    rgb = data.get("color") or {}
    if kelvin:
        out["mirek"] = kelvin_to_mirek(kelvin)
    elif rgb:
        out["color"] = "#{:02x}{:02x}{:02x}".format(rgb.get("r", 0), rgb.get("g", 0), rgb.get("b", 0))
    return out
