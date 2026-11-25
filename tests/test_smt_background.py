import asyncio

from hub.adapters import smt_adapter


class SlowMeter:
    esiid, meter = "1", "M1"

    def __init__(self):
        self.reads = 0

    async def read_meter(self, client):
        self.reads += 1
        await asyncio.sleep(3600)          # Smart Meter Texas polling the meter

    async def get_15min(self, client, prevdays=1):
        self.read_15min_consumption = []


def test_device_list_does_not_wait_for_a_slow_meter_read(tmp_path, monkeypatch):
    async def run():
        a = smt_adapter.SmartMeterTexasAdapter(secrets=type("S", (), {"get": lambda self, k: {"username": "u", "password": "p"}})())
        meter = SlowMeter()
        a._client = object()
        a._meters = [meter]
        devices = await asyncio.wait_for(a.devices(), 2)
        assert len(devices) == 1 and "Waiting" in devices[0].media
        await asyncio.sleep(0)
        assert meter.reads == 1
        devices = await asyncio.wait_for(a.devices(), 2)   # a second request doesn't start another read
        assert meter.reads == 1
        a._session = None
        await a.close()
    asyncio.run(run())
