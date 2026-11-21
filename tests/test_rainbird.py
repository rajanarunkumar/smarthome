"""Rain Bird, with a fake controller standing in for pyrainbird."""

import asyncio
from datetime import time as dtime, timedelta
from types import SimpleNamespace

import pytest

from hub.adapters import rainbird_adapter
from hub.adapters.rainbird_adapter import RainBirdAdapter, describe_program, status_text
from hub.config import Secrets
from hub.models import HubError, StateIn


def program(name="A", freq="CUSTOM", days=("MONDAY", "FRIDAY", "WEDNESDAY"), starts=(dtime(5, 0), dtime(18, 30)),
            minutes=(10, 15, 20), period=None):
    return SimpleNamespace(
        name=name, frequency=SimpleNamespace(name=freq), period=period, starts=list(starts),
        days_of_week=[SimpleNamespace(name=d) for d in days],
        durations=[SimpleNamespace(duration=timedelta(minutes=m)) for m in minutes])


def test_program_descriptions():
    assert describe_program(program()) == {"label": "Program A", "value": "5:00 AM, 6:30 PM · Mon Wed Fri · 45 min"}
    assert describe_program(program(freq="CYCLIC", period=3))["value"].endswith("· Every 3 days · 45 min")
    assert describe_program(program(freq="OFF")) is None
    assert describe_program(program(starts=())) is None


def state(**kw):
    base = dict(irrigation_state=0, active_station=0, remaining_runtime=0, delay_setting=0, sensor_state=0)
    return SimpleNamespace(**{**base, **kw})


def test_status_text():
    label = lambda z: f"Zone {z}"
    assert status_text(state(irrigation_state=1, active_station=3, remaining_runtime=700), label) == "Watering Zone 3 · 12 min left"
    assert status_text(state(delay_setting=2, sensor_state=1), label) == "Idle · Rain delay 2 days · Rain sensor wet"


class FakeController:
    def __init__(self):
        self.calls = []
        self.st = state(irrigation_state=1, active_station=2, remaining_runtime=300)

    async def get_combined_controller_state(self): return self.st
    async def get_schedule(self): return SimpleNamespace(programs=[program()])
    async def irrigate_zone(self, zone, minutes): self.calls.append(("run", zone, minutes))
    async def stop_irrigation(self): self.calls.append(("stop",))
    async def set_rain_delay(self, days): self.calls.append(("delay", days))


@pytest.fixture
def adapter(tmp_path):
    a = RainBirdAdapter(Secrets(tmp_path / "s.json"))
    a.secrets.set("rainbird", {"host": "192.168.1.40", "password": "pw", "zone_names": {}})
    a._ctl, a._zones, a._model = FakeController(), [1, 2, 3], "ESP-TM2"
    return a


def test_device_shows_running_zone_and_programs(adapter):
    adapter.name_zone(2, "Front lawn")
    [d] = asyncio.run(adapter.devices())
    assert (d.kind, d.on, d.media, d.model) == ("sprinkler", True, "Watering Zone 2 (Front lawn) · 5 min left", "ESP-TM2")
    assert [(z["id"], z["running"], z["name"]) for z in d.zones] == [("1", False, ""), ("2", True, "Front lawn"), ("3", False, "")]
    assert d.details == [{"label": "Program A", "value": "5:00 AM, 6:30 PM · Mon Wed Fri · 45 min"}]


def test_commands(adapter):
    ctl = adapter._ctl
    asyncio.run(adapter.set_state(rainbird_adapter.DEVICE_ID, StateIn(zone=3, minutes=15)))
    asyncio.run(adapter.set_state(rainbird_adapter.DEVICE_ID, StateIn(action="stop")))
    asyncio.run(adapter.set_state(rainbird_adapter.DEVICE_ID, StateIn(rain_delay_days=2)))
    asyncio.run(adapter.set_state(rainbird_adapter.DEVICE_ID, StateIn(rain_delay_days=0)))
    assert ctl.calls == [("run", 3, 15), ("stop",), ("delay", 2), ("delay", 0)]
    with pytest.raises(HubError):  # zone 9 isn't wired on this controller
        asyncio.run(adapter.set_state(rainbird_adapter.DEVICE_ID, StateIn(zone=9, minutes=5)))
    with pytest.raises(HubError):  # nothing to do
        asyncio.run(adapter.set_state(rainbird_adapter.DEVICE_ID, StateIn()))
