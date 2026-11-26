"""Convert between the hub's neutral schedule shape and each brand's native format.

Kasa: rules stored on the plug ("schedule" module, local protocol).
  wday is Sunday-first; smin is minutes after midnight; stime_opt 0=time,
  1=sunrise, 2=sunset; sact 0=off, 1=on, 3=dimmer state in s_dimmer.
Hue: v1 bridge schedules. localtime "W<mask>/Thh:mm:ss" where the mask bits
  are 0MTWTFSS (Monday=64 ... Sunday=1).
"""

from __future__ import annotations

import re

from .models import DAYS, Action, HubError, Trigger

HUB_MARK = "smarthome-hub"

_KASA_TRIGGER = {0: "time", 1: "sunrise", 2: "sunset"}
_KASA_TRIGGER_OPT = {v: k for k, v in _KASA_TRIGGER.items()}


def _hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _pct(value: int | None) -> int | None:
    return None if value is None else max(1, min(100, int(value)))


# Kasa
def kasa_wday(days: list[str]) -> list[int]:
    sunday_first = DAYS[6:] + DAYS[:6]
    return [1 if d in days else 0 for d in sunday_first]


def days_from_kasa_wday(wday: list[int]) -> list[str]:
    sunday_first = DAYS[6:] + DAYS[:6]
    chosen = {d for d, flag in zip(sunday_first, wday) if flag}
    return [d for d in DAYS if d in chosen]


def parse_kasa_rule(rule: dict) -> tuple[Action, Trigger, list[str]] | None:
    """Return (action, trigger, days), or None if the rule uses a format we don't know."""
    sact = rule.get("sact")
    dimmer = rule.get("s_dimmer") or {}
    if sact == 0:
        action = Action(on=False)
    elif sact == 1:
        action = Action(on=True)
    elif sact == 3:
        on = bool(dimmer.get("on_off", 1))
        action = Action(on=on, brightness=_pct(dimmer.get("brightness")) if on else None)
    else:
        return None

    kind = _KASA_TRIGGER.get(rule.get("stime_opt", 0))
    if kind is None:
        return None
    if kind == "time":
        trigger = Trigger(type="time", time=_hhmm(int(rule.get("smin", 0))))
    else:
        trigger = Trigger(type=kind, offset_min=int(rule.get("soffset", 0)))
    return action, trigger, days_from_kasa_wday(rule.get("wday", [1] * 7))


def build_kasa_rule(
    name: str,
    action: Action,
    trigger: Trigger,
    days: list[str],
    enabled: bool,
    is_dimmer: bool,
    sun_smin: int | None = None,
) -> dict:
    """Build a rule like the Kasa app's. add_rule also requires the end-time fields."""
    rule: dict = {
        "name": name,
        "enable": int(enabled),
        "wday": kasa_wday(days),
        "repeat": 1,
        "stime_opt": _KASA_TRIGGER_OPT[trigger.type],
        "eact": -1,
        "etime_opt": -1,
        "emin": 0,
    }
    if trigger.type == "time":
        rule["smin"] = _minutes(trigger.time or "00:00")
    else:
        # The plug recomputes sunrise/sunset from its own location; smin is
        # just today's estimate, copied from an existing rule when available.
        rule["smin"] = ((sun_smin or 0) + trigger.offset_min) % 1440
        rule["soffset"] = trigger.offset_min

    if is_dimmer and action.on and action.brightness:
        rule["sact"] = 3
        rule["s_dimmer"] = {"on_off": 1, "brightness": action.brightness, "transition_period": 1000}
    elif is_dimmer and not action.on:
        rule["sact"] = 0
        rule["s_dimmer"] = {"transition_period": 1000}
    else:
        rule["sact"] = 1 if action.on else 0
    return rule


# Hue (v1 bridge schedules)
_LOCALTIME = re.compile(r"^W(\d+)/T(\d{2}):(\d{2}):\d{2}$")
_ADDRESS = re.compile(r"^/api/[^/]+(/(?:lights/\d+/state|groups/\d+/action))$")


def hue_mask(days: list[str]) -> int:
    return sum(1 << (6 - DAYS.index(d)) for d in days)


def days_from_hue_mask(mask: int) -> list[str]:
    return [d for i, d in enumerate(DAYS) if mask & (1 << (6 - i))]


def parse_hue_localtime(localtime: str) -> tuple[str, list[str]] | None:
    m = _LOCALTIME.match(localtime or "")
    if not m:
        return None
    return f"{m.group(2)}:{m.group(3)}", days_from_hue_mask(int(m.group(1)))


def parse_hue_command(command: dict) -> tuple[str, Action] | None:
    """Return (target, action) where target is e.g. '/lights/5' or '/groups/1'."""
    m = _ADDRESS.match(command.get("address", ""))
    body = command.get("body") or {}
    if not m or "on" not in body:
        return None
    target = m.group(1).rsplit("/", 1)[0]
    on = bool(body["on"])
    bri = body.get("bri")
    brightness = _pct(round(bri * 100 / 254)) if (on and bri is not None) else None
    return target, Action(on=on, brightness=brightness)


def build_hue_schedule(
    app_key: str, id_v1: str, name: str, action: Action, trigger: Trigger, days: list[str], enabled: bool
) -> dict:
    if trigger.type != "time":
        raise HubError(400, "Hue Bridge schedules support set times only, not sunrise or sunset.")
    body: dict = {"on": action.on}
    if action.on and action.brightness:
        body["bri"] = max(1, round(action.brightness * 254 / 100))
    return {
        "name": name[:32],
        "description": HUB_MARK,
        "command": {"address": f"/api/{app_key}{id_v1}/state", "method": "PUT", "body": body},
        "localtime": f"W{hue_mask(days):03d}/T{trigger.time}:00",
        "status": "enabled" if enabled else "disabled",
        "recycle": False,
    }
