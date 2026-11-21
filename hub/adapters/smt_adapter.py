"""Electricity from Smart Meter Texas (smartmetertexas.com), the state portal
that TXU and every other Texas retailer read your meter from. Uses
smart-meter-texas, the library behind Home Assistant's Smart Meter Texas
integration. Needs a free Smart Meter Texas account (separate from TXU's).

Smart Meter Texas allows about 2 on-demand meter reads an hour, so the hub
reads the meter once an hour and yesterday's 15-minute usage once a day.
The password is kept in ~/.smarthome/secrets.json: Smart Meter Texas logins
expire and the library signs in again with it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from ..config import Secrets
from ..models import Device, HubError
from .base import Adapter

log = logging.getLogger(__name__)
READ_SECONDS = 3600
DAILY_SECONDS = 6 * 3600

try:
    import aiohttp
    from smart_meter_texas import Account, Client, ClientSSLContext
    from smart_meter_texas.exceptions import SmartMeterTexasAuthError
    from smart_meter_texas.exceptions import SmartMeterTexasException as SmartMeterTexasAPIError
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

    class SmartMeterTexasAPIError(Exception):
        pass

    class SmartMeterTexasAuthError(SmartMeterTexasAPIError):
        pass


def parse_intervals(intervals: Any) -> list[tuple[str, float]]:
    """[("MM/DD/YYYY H:MM", "kWh"), …] from smart-meter-texas -> [("YYYY-MM-DDTHH:MM", kWh)]."""
    out = []
    for item in intervals or []:
        try:
            when = datetime.strptime(item[0], "%m/%d/%Y %H:%M")
            out.append((when.strftime("%Y-%m-%dT%H:%M"), float(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def day_total(intervals: Any) -> float | None:
    """Sum kWh from read_15min_consumption, which is a list of (time, kWh) pairs or dicts."""
    if not intervals:
        return None
    total = 0.0
    for item in intervals:
        value = item[1] if isinstance(item, (list, tuple)) else (item or {}).get("consumption", (item or {}).get("kwh"))
        try:
            total += float(value)
        except (TypeError, ValueError):
            continue
    return round(total, 1)


class SmartMeterTexasAdapter(Adapter):
    brand = "smt"
    label = "Smart Meter Texas"
    prefixes = ("smt",)
    can_rename = False

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._session: Any = None
        self._client: Any = None
        self._meters: list[Any] = []
        self._read_at = 0.0
        self._daily_at = 0.0
        self._yesterday: dict[str, float | None] = {}
        self._lock = asyncio.Lock()

    @property
    def config(self) -> dict:
        return self.secrets.get("smt") or {}

    @property
    def active(self) -> bool:
        return AVAILABLE and bool(self.config.get("password"))

    async def _ensure(self, cfg: dict | None = None) -> None:
        cfg = cfg or self.config
        if self._client is None:
            self._session = aiohttp.ClientSession()
            ssl_context = await asyncio.to_thread(ClientSSLContext().create_ssl_context)
            self._client = Client(self._session, Account(cfg["username"], cfg["password"]), ssl_context)
            await self._client.authenticate()
            self._meters = await self._client.account.fetch_meters(self._client)

    async def connect(self, username: str, password: str) -> dict:
        if not AVAILABLE:
            raise HubError(501, "Smart Meter Texas support isn't installed. Run pip install -r requirements.txt.")
        cfg = {"username": username.strip(), "password": password}
        await self.close()
        try:
            await self._ensure(cfg)
        except SmartMeterTexasAuthError as exc:
            await self.close()
            raise HubError(401, "Smart Meter Texas didn't accept that username and password. "
                                "Use your smartmetertexas.com login, not your TXU one.") from exc
        except SmartMeterTexasAPIError as exc:
            await self.close()
            raise HubError(502, f"Smart Meter Texas: {exc}") from exc
        self.secrets.set("smt", cfg)
        return {"connected": True, "devices": len(self._meters)}

    async def disconnect(self) -> None:
        await self.close()
        self.secrets.set("smt", None)

    async def _update(self) -> None:
        async with self._lock:
            await self._ensure()
            now = time.monotonic()
            if now - self._read_at > READ_SECONDS or not self._read_at:
                self._read_at = now
                for meter in self._meters:
                    try:
                        await meter.read_meter(self._client)
                    except SmartMeterTexasAPIError as exc:
                        log.warning("Smart Meter Texas read failed: %s", exc)
            if now - self._daily_at > DAILY_SECONDS or not self._daily_at:
                self._daily_at = now
                for meter in self._meters:
                    try:
                        await meter.get_15min(self._client)
                        self._yesterday[meter.esiid] = day_total(meter.read_15min_consumption)
                    except SmartMeterTexasAPIError as exc:
                        log.warning("Smart Meter Texas daily usage failed: %s", exc)

    async def devices(self) -> list[Device]:
        try:
            await self._update()
        except SmartMeterTexasAuthError as exc:
            await self.close()
            raise HubError(401, "Smart Meter Texas sign-in failed. Connect again in Settings.") from exc
        out = []
        for meter in self._meters:
            try:  # both raise until the first successful read
                reading, when = meter.reading, meter.reading_datetime
            except (AttributeError, KeyError, TypeError, ValueError):
                reading = when = None
            yesterday = self._yesterday.get(meter.esiid)
            details = []
            if yesterday is not None:
                details.append({"label": "Yesterday", "value": f"{yesterday:g} kWh"})
            if reading is not None:
                details.append({"label": "Meter reading", "value": f"{float(reading):,.0f} kWh"})
            if when:
                details.append({"label": "Read at", "value": str(when)[:16]})
            out.append(Device(
                id=f"smt:{meter.esiid}", brand=self.brand, kind="meter", name="Electricity",
                model=f"Meter {meter.meter}", online=reading is not None,
                media=f"Yesterday {yesterday:g} kWh" if yesterday is not None else "Waiting for the first reading",
                details=details, capabilities=[],
            ))
        return out

    async def intervals(self, days: int = 28, have: set[str] = frozenset()) -> list[tuple[str, float]]:
        """15-minute kWh for the last `days` days, skipping days already stored. Slots are labeled by start time."""
        await self._ensure()
        rows: dict[str, float] = {}
        for back in range(1, days + 1):
            day = date.today() - timedelta(days=back)
            if day.isoformat() in have:
                continue
            for meter in self._meters:
                try:
                    await meter.get_15min(self._client, prevdays=back)
                except SmartMeterTexasAPIError as exc:
                    log.info("Smart Meter Texas has no 15-minute data for %s: %s", day, exc)
                    continue
                for slot, kwh in parse_intervals(meter.read_15min_consumption):
                    rows[slot] = rows.get(slot, 0.0) + kwh
            await asyncio.sleep(1)  # be gentle with Smart Meter Texas
        return sorted(rows.items())

    def names(self) -> dict[str, str]:
        return {f"smt:{m.esiid}": "Electricity" for m in self._meters}

    async def status(self) -> dict:
        return {"connected": self.active, "available": AVAILABLE, "devices": len(self._meters)}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = self._client = None
        self._meters = []
