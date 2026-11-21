"""Water use from My Water Advisor 2 (mywateradvisor2.com), the portal the City
of McKinney uses for its smart water meters.

There's no published API. The hub makes the same calls the My Water Advisor
website makes (worked out from the site's own JavaScript): sign in, list
meters, then daily consumption. It only reads. If the city or the vendor
changes the site, this can break until it's updated.

The password is kept in ~/.smarthome/secrets.json, because the site's login
token expires and the hub signs in again with it.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date, timedelta
from typing import Any

import httpx

from ..config import Secrets
from ..models import Device, HubError
from .base import Adapter

log = logging.getLogger(__name__)
BASE_URL = "https://customerportal-api.harmonyencoremdm.com"
APP_ID = "3a869241-d476-40f6-a923-d789d63db11d"  # the website's own public app id
REFRESH_SECONDS = 3 * 3600


def _items(response: Any) -> list[dict]:
    """Responses come as a list or wrapped in {"data": [...]} / {"consumptionData": [...]}."""
    if isinstance(response, dict):
        for key in ("data", "consumptionData", "consumptions", "meters"):
            if isinstance(response.get(key), list):
                return response[key]
        return []
    return response if isinstance(response, list) else []


def daily_usage(items: list[dict]) -> list[tuple[str, float]]:
    """[(YYYY-MM-DD, amount)] from daily items like {"consDate": ..., "cons": ...}."""
    out = []
    for item in items:
        day, amount = item.get("consDate") or item.get("date"), item.get("cons")
        try:
            out.append((str(day)[:10], float(amount)))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def hourly_usage(items: list[dict]) -> list[tuple[str, float]]:
    """[('YYYY-MM-DDTHH', amount)] from hourly items like {"consDate": "2026-11-20T05:00:00", "cons": 12}."""
    out = []
    for item in items:
        stamp, amount = str(item.get("consDate") or item.get("date") or ""), item.get("cons")
        if len(stamp) < 13:
            continue
        try:
            out.append((stamp[:10] + "T" + stamp[11:13], float(amount)))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def unit_of(items: list[dict]) -> str:
    unit = next((i.get("units") for i in items if i.get("units")), None)
    return str(unit).lower() if unit else "gal"


def _mdy(d: date) -> str:
    return d.strftime("%m-%d-%Y")


class WaterAdvisorAdapter(Adapter):
    brand = "water"
    label = "My Water Advisor"
    prefixes = ("water",)
    can_rename = False

    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=20,
                                       headers={"Accept": "application/json", "Content-Type": "application/json"})
        self._token: str | None = None
        self._meters: list[dict] = []
        self._usage: dict[str, list[dict]] = {}
        self._at = float("-inf")

    @property
    def config(self) -> dict:
        return self.secrets.get("water") or {}

    @property
    def active(self) -> bool:
        return bool(self.config.get("password"))

    async def _login(self, cfg: dict) -> str:
        body = {"email": cfg["email"], "type": 1, "pw": cfg["password"], "deviceId": cfg["device_id"],
                "osType": 3, "app": APP_ID}
        try:
            r = await self._http.post("/consumer/login", json=body, headers={"x-app-id": APP_ID})
        except httpx.HTTPError as exc:
            raise HubError(504, f"Couldn't reach My Water Advisor: {exc}") from exc
        data = r.json() if r.content else {}
        if not data.get("token"):
            raise HubError(401, f"My Water Advisor didn't accept the sign-in: {data.get('error') or r.status_code}")
        return data["token"]

    async def _get(self, path: str) -> Any:
        for attempt in range(2):
            if self._token is None:
                self._token = await self._login(self.config)
            r = await self._http.get(path, headers={"x-access-token": self._token})
            if r.status_code == 401 and attempt == 0:
                self._token = None  # expired: sign in again once
                continue
            if r.status_code >= 400:
                raise HubError(502, f"My Water Advisor said {r.status_code}")
            return r.json() if r.content else None
        raise HubError(401, "My Water Advisor sign-in failed. Connect again in Settings.")

    async def connect(self, email: str, password: str) -> dict:
        cfg = {"email": email.strip(), "password": password,
               "device_id": self.config.get("device_id") or str(uuid.uuid4())}
        self._token = await self._login(cfg)
        self.secrets.set("water", cfg)
        self._at = float("-inf")
        found = await self.devices()
        return {"connected": True, "devices": len(found)}

    async def disconnect(self) -> None:
        self.secrets.set("water", None)
        self._token, self._meters, self._usage = None, [], {}

    async def _refresh(self) -> None:
        if time.monotonic() - self._at < REFRESH_SECONDS and self._meters:
            return
        self._meters = _items(await self._get("/consumer/meters"))
        end = date.today()
        start = end - timedelta(days=31)
        for meter in self._meters:
            count = meter.get("meterCount")
            path = f"/consumption/daily/{count}/{_mdy(start)}/{_mdy(end)}" if count is not None \
                else f"/consumption/daily/{_mdy(start)}/{_mdy(end)}"
            try:
                self._usage[str(count)] = _items(await self._get(path))
            except HubError as exc:
                log.warning("Water usage for meter %s failed: %s", count, exc.message)
        self._at = time.monotonic()

    async def devices(self) -> list[Device]:
        await self._refresh()
        out = []
        for meter in self._meters:
            count = str(meter.get("meterCount"))
            items = self._usage.get(count, [])
            days, unit = daily_usage(items), unit_of(items)
            yesterday = days[-2][1] if len(days) >= 2 else (days[-1][1] if days else None)
            week = sum(v for _, v in days[-7:]) if days else None
            month = sum(v for d, v in days if d[:7] == date.today().isoformat()[:7]) if days else None
            details = [{"label": label, "value": f"{value:,.0f} {unit}"} for label, value in
                       (("Latest full day", yesterday), ("Last 7 days", week), ("This month", month)) if value is not None]
            out.append(Device(
                id=f"water:{count}", brand=self.brand, kind="meter", name="Water",
                model=" · ".join(str(x) for x in (meter.get("meterSn"), meter.get("fullAddress")) if x) or None,
                online=bool(days), details=details, capabilities=[],
                media=f"Latest day {yesterday:,.0f} {unit}" if yesterday is not None else "No usage data yet",
            ))
        return out

    async def history(self, days: int = 14) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
        """(daily rows, hourly rows) for all meters added together, for the analytics history."""
        await self._refresh()
        end = date.today()
        start = end - timedelta(days=days)
        daily: dict[str, float] = {}
        hourly: dict[str, float] = {}
        for meter in self._meters:
            count = str(meter.get("meterCount"))
            for day, amount in daily_usage(self._usage.get(count, [])):
                daily[day] = daily.get(day, 0) + amount
            try:
                items = _items(await self._get(f"/consumption/hourly/{count}/{start.isoformat()}/{end.isoformat()}"))
            except HubError as exc:
                log.warning("Hourly water for meter %s failed: %s", count, exc.message)
                continue
            for hour, amount in hourly_usage(items):
                hourly[hour] = hourly.get(hour, 0) + amount
        return sorted(daily.items()), sorted(hourly.items())

    def names(self) -> dict[str, str]:
        return {f"water:{m.get('meterCount')}": "Water" for m in self._meters}

    async def status(self) -> dict:
        return {"connected": self.active, "devices": len(self._meters)}

    async def close(self) -> None:
        await self._http.aclose()
