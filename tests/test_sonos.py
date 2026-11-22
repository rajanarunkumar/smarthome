"""Sonos alarm recurrence <-> hub days."""

from hub.adapters.sonos_adapter import days_to_recurrence, recurrence_to_days


def test_days_to_recurrence_uses_sunday_zero():
    assert days_to_recurrence(["mon", "tue", "wed", "thu", "fri"]) == "ON_12345"
    assert days_to_recurrence(["sun", "sat"]) == "ON_06"


def test_recurrence_keywords_and_digits():
    assert recurrence_to_days("DAILY") == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert recurrence_to_days("WEEKENDS") == ["sat", "sun"]
    assert recurrence_to_days("ONCE") == []
    assert recurrence_to_days("ON_135") == ["mon", "wed", "fri"]


def test_round_trip():
    days = ["tue", "thu", "sun"]
    assert recurrence_to_days(days_to_recurrence(days)) == days
