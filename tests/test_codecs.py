"""Schedule and color conversions. Kasa fixtures are real rule shapes written by the Kasa app."""

import pytest

from hub.color import hex_to_xy, mirek_to_hex, xy_to_hex
from hub.models import Action, HubError, Trigger
from hub.schedule_codec import (
    HUB_MARK,
    build_hue_schedule,
    build_kasa_rule,
    days_from_hue_mask,
    days_from_kasa_wday,
    hue_mask,
    kasa_wday,
    parse_hue_command,
    parse_hue_localtime,
    parse_kasa_rule,
)

ALL = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Kasa: days
def test_kasa_wday_is_sunday_first():
    assert kasa_wday(["sun"]) == [1, 0, 0, 0, 0, 0, 0]
    assert kasa_wday(["mon", "fri"]) == [0, 1, 0, 0, 0, 1, 0]
    assert days_from_kasa_wday([1, 0, 0, 0, 0, 0, 1]) == ["sat", "sun"]
    assert days_from_kasa_wday(kasa_wday(ALL)) == ALL


# Kasa: parsing rules from the Kasa app
def test_parse_plug_sunset_on():
    rule = {"id": "x", "name": "name", "enable": 1, "wday": [1] * 7, "stime_opt": 2,
            "smin": 1041, "soffset": 0, "sact": 1, "eact": -1, "repeat": 1}
    action, trigger, days = parse_kasa_rule(rule)
    assert action == Action(on=True)
    assert trigger.type == "sunset" and trigger.offset_min == 0
    assert days == ALL


def test_parse_plug_time_off():
    rule = {"id": "x", "name": "name", "enable": 1, "wday": [1] * 7, "stime_opt": 0,
            "smin": 1350, "sact": 0, "eact": -1, "repeat": 1}
    action, trigger, _ = parse_kasa_rule(rule)
    assert action == Action(on=False)
    assert trigger.time == "22:30"


def test_parse_dimmer_brightness_rule():
    rule = {"id": "x", "name": "name", "enable": 1, "wday": [1] * 7, "stime_opt": 0, "smin": 1260,
            "sact": 3, "s_dimmer": {"on_off": 1, "brightness": 25, "transition_period": 1000},
            "eact": -1, "repeat": 1}
    action, trigger, _ = parse_kasa_rule(rule)
    assert action == Action(on=True, brightness=25)
    assert trigger.time == "21:00"


def test_parse_dimmer_off_rule():
    rule = {"id": "x", "name": "name", "enable": 1, "wday": [1] * 7, "stime_opt": 0, "smin": 120,
            "sact": 0, "s_dimmer": {"transition_period": 1000}, "eact": -1, "repeat": 1}
    action, trigger, _ = parse_kasa_rule(rule)
    assert action == Action(on=False)
    assert trigger.time == "02:00"


def test_parse_unknown_action_returns_none():
    assert parse_kasa_rule({"sact": 7, "stime_opt": 0, "smin": 0, "wday": [1] * 7}) is None


# Kasa: building rules
def test_build_plug_time_rule():
    rule = build_kasa_rule("Porch", Action(on=True), Trigger(type="time", time="07:15"),
                           ["mon", "tue"], True, is_dimmer=False)
    assert rule == {"name": "Porch", "enable": 1, "wday": [0, 1, 1, 0, 0, 0, 0], "repeat": 1,
                    "stime_opt": 0, "eact": -1, "etime_opt": -1, "emin": 0, "smin": 435, "sact": 1}


def test_build_dimmer_rule_round_trips():
    spec = (Action(on=True, brightness=40), Trigger(type="sunset", offset_min=-15), ["sat", "sun"])
    rule = build_kasa_rule("Evening", *spec, enabled=False, is_dimmer=True, sun_smin=1041)
    assert rule["sact"] == 3 and rule["s_dimmer"]["brightness"] == 40
    assert rule["smin"] == 1026 and rule["soffset"] == -15 and rule["enable"] == 0
    assert parse_kasa_rule(rule) == spec


def test_build_dimmer_off_matches_kasa_app_shape():
    rule = build_kasa_rule("Off", Action(on=False), Trigger(type="time", time="02:00"), ALL, True, is_dimmer=True)
    assert rule["sact"] == 0 and rule["s_dimmer"] == {"transition_period": 1000}


# Hue
def test_hue_mask():
    assert hue_mask(ALL) == 127
    assert hue_mask(["mon"]) == 64 and hue_mask(["sun"]) == 1
    assert hue_mask(["mon", "tue", "wed", "thu", "fri"]) == 124
    assert days_from_hue_mask(3) == ["sat", "sun"]


def test_parse_hue_localtime():
    assert parse_hue_localtime("W124/T07:30:00") == ("07:30", ["mon", "tue", "wed", "thu", "fri"])
    assert parse_hue_localtime("PT00:10:00") is None


def test_build_and_parse_hue_schedule():
    body = build_hue_schedule("KEY", "/lights/5", "Wake", Action(on=True, brightness=50),
                              Trigger(type="time", time="06:45"), ["mon", "wed"], True)
    assert body["localtime"] == "W080/T06:45:00"
    assert body["description"] == HUB_MARK
    assert body["command"]["address"] == "/api/KEY/lights/5/state"
    assert body["command"]["body"] == {"on": True, "bri": 127}
    target, action = parse_hue_command(body["command"])
    assert target == "/lights/5" and action == Action(on=True, brightness=50)


def test_hue_rejects_sunset():
    with pytest.raises(HubError):
        build_hue_schedule("K", "/lights/1", "x", Action(on=True), Trigger(type="sunset"), ALL, True)


# Color
def test_color_round_trip_is_close():
    for hexval in ["#ff0000", "#00ff00", "#0000ff", "#ffa500"]:
        x, y = hex_to_xy(hexval)
        back = xy_to_hex(x, y)
        orig = [int(hexval[i:i + 2], 16) for i in (1, 3, 5)]
        got = [int(back[i:i + 2], 16) for i in (1, 3, 5)]
        assert max(abs(a - b) for a, b in zip(orig, got)) <= 3, (hexval, back)


def test_mirek_warm_is_redder_than_cool():
    warm, cool = mirek_to_hex(454), mirek_to_hex(153)
    assert int(warm[5:7], 16) < int(cool[5:7], 16)
