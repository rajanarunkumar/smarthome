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

import asyncio
import logging
import re
import statistics
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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


STAMP_FIELDS = ("consDate", "date", "readDate", "dateTime", "timestamp", "time")
AMOUNT_FIELDS = ("cons", "consumption", "usage", "value", "volume")
US_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
              "%m-%d-%Y %H:%M:%S", "%m-%d-%Y %H:%M", "%m/%d/%Y", "%m-%d-%Y")
GAL_PER = {  # one of this unit, in US gallons
    "gal": 1.0, "gals": 1.0, "gallon": 1.0, "gallons": 1.0,
    "cf": 7.48052, "cu ft": 7.48052, "cuft": 7.48052, "ft3": 7.48052, "ft³": 7.48052, "cubic feet": 7.48052, "cubic foot": 7.48052,
    "ccf": 748.052, "hcf": 748.052, "100 cf": 748.052, "100 cubic feet": 748.052,
    "kgal": 1000.0, "kgals": 1000.0, "1000 gal": 1000.0, "1000 gallons": 1000.0, "thousand gallons": 1000.0,
    "m3": 264.172, "m³": 264.172, "cubic meters": 264.172, "l": 0.264172, "liter": 0.264172, "liters": 0.264172, "litres": 0.264172,
}


def unit_factor(unit: str | None) -> float | None:
    """Gallons per meter unit; 1.0 when the meter names no unit; None for a unit the hub doesn't know."""
    if not unit:
        return 1.0
    return GAL_PER.get(re.sub(r"[.\s]+", " ", str(unit).strip().lower()).strip())


def _raw_unit(items: list[dict]) -> str | None:
    return next((str(i["units"]) for i in items if i.get("units")), None)


def _pick(item: dict, names: tuple[str, ...]) -> Any:
    return next((item[n] for n in names if item.get(n) is not None), None)


def parse_stamp(value: Any, tz) -> tuple[datetime, bool] | None:
    """(local naive datetime, has_time). Reads ISO (a Z or offset is converted to local), US-style
    dates and date-times, and epoch seconds or milliseconds. None when it can't be read."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{9,13}(\.\d+)?", value.strip())):
        n = float(value)
        n = n / 1000 if n > 1e11 else n
        if n < 1e8:
            return None
        return datetime.fromtimestamp(n, tz=ZoneInfo("UTC")).astimezone(tz).replace(tzinfo=None), True
    text = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        has_time = len(text) >= 13 and text[10] in "T "
        try:
            if text.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", text[10:]):
                return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(tz).replace(tzinfo=None), has_time
            return datetime.fromisoformat(text[:19] if has_time else text[:10]), has_time
        except ValueError:
            return None
    for fmt in US_FORMATS:
        try:
            return datetime.strptime(text, fmt), ("%H" in fmt or "%I" in fmt)
        except ValueError:
            continue
    return None


def local_hour(stamp: Any, tz) -> str | None:
    """'YYYY-MM-DDTHH' in the home's time zone, or None if the stamp has no readable time."""
    parsed = parse_stamp(stamp, tz)
    return parsed[0].strftime("%Y-%m-%dT%H") if parsed and parsed[1] else None


def day_key(stamp: Any, tz) -> str | None:
    """YYYY-MM-DD. An ISO date is taken as written (midnight UTC stamps mean that date)."""
    if isinstance(stamp, str) and re.match(r"^\d{4}-\d{2}-\d{2}", stamp.strip()):
        return stamp.strip()[:10]
    parsed = parse_stamp(stamp, tz)
    return parsed[0].strftime("%Y-%m-%d") if parsed else None


def daily_usage(items: list[dict], factor: float = 1.0, tz=None) -> list[tuple[str, float]]:
    """[(YYYY-MM-DD, gallons)] from daily items like {"consDate": ..., "cons": ...}."""
    tz = tz or ZoneInfo("America/Chicago")
    out = []
    for item in items:
        day, amount = day_key(_pick(item, STAMP_FIELDS), tz), _pick(item, AMOUNT_FIELDS)
        try:
            if day and float(amount) >= 0:
                out.append((day, float(amount) * factor))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def hourly_rows(items: list[dict], tz=None, factor: float = 1.0) -> tuple[list[tuple[str, float]], list]:
    """([('YYYY-MM-DDTHH', gallons)], unreadable stamps). Two readings in one local hour (the clocks going
    back) are added together."""
    tz = tz or ZoneInfo("America/Chicago")
    rows: dict[str, float] = {}
    bad = []
    for item in items:
        stamp, amount = _pick(item, STAMP_FIELDS), _pick(item, AMOUNT_FIELDS)
        hour = local_hour(stamp, tz)
        try:
            value = float(amount)
        except (TypeError, ValueError):
            value = -1.0
        if hour is None or value < 0:
            bad.append(stamp)
            continue
        rows[hour] = rows.get(hour, 0.0) + value * factor
    return sorted(rows.items()), bad


def hourly_usage(items: list[dict], tz=None, factor: float = 1.0) -> list[tuple[str, float]]:
    return hourly_rows(items, tz, factor)[0]


def looks_cumulative(values: list[float]) -> bool:
    """A running meter total, not use per hour: never goes down, large next to its own steps."""
    if len(values) < 12:
        return False
    steps = [b - a for a, b in zip(values, values[1:])]
    return min(steps) >= 0 and max(steps) > 0 and statistics.median(values) > 10 * max(steps)


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone name: fall back to Central
        return ZoneInfo("America/Chicago")


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
        self.diagnostics: dict = {}   # what the last hourly fetch managed, shown in Insights → Data check
        self._hourly_fmt, self._hourly_fmt_proven = "iso", False  # which date format the hourly URL accepts

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

    async def _hourly_items(self, count: str, day: date) -> tuple[list[dict] | None, str | None]:
        """One day's hourly items. Until one date format has produced data, a refused or empty answer is
        retried with the other (ISO 2026-11-20, then US 11-20-2026), since the daily endpoint uses US dates."""
        other = "mdy" if self._hourly_fmt == "iso" else "iso"
        order = [self._hourly_fmt] if self._hourly_fmt_proven else [self._hourly_fmt, other]
        error, answered = None, False
        for fmt in order:
            text = day.isoformat() if fmt == "iso" else _mdy(day)
            try:
                items = _items(await self._get(f"/consumption/hourly/{count}/{text}/{text}"))
            except HubError as exc:
                error = exc.message
                continue
            if items:
                self._hourly_fmt, self._hourly_fmt_proven = fmt, True
                return items, None
            answered = True
        return ([], None) if answered else (None, error)

    async def history(self, days: int = 14, tz_name: str = "America/Chicago"
                      ) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
        """(daily rows, hourly rows) in gallons, all meters added together, for the analytics history.

        Hourly readings are requested one day at a time, the way the My Water Advisor website does (start
        and end date equal). Everything is converted to gallons from the meter's own unit.
        """
        await self._refresh()
        tz = _zone(tz_name)
        end = date.today()
        daily: dict[str, float] = {}
        hourly: dict[str, float] = {}
        diag: dict[str, Any] = {
            "checked": datetime.now().isoformat(timespec="minutes"), "hourly_days_requested": 0, "hourly_days_with_data": 0,
            "hourly_failures": 0, "sample_stamp": None, "last_error": None, "unit": None, "unit_factor": 1.0,
            "unit_unknown": None, "unparsed_items": 0, "bad_stamp": None, "cumulative": False}
        for meter in self._meters:
            count = str(meter.get("meterCount"))
            daily_items = self._usage.get(count, [])
            daily_unit = _raw_unit(daily_items)
            for day, amount in daily_usage(daily_items, unit_factor(daily_unit) or 1.0, tz):
                daily[day] = daily.get(day, 0) + amount
            meter_hours: dict[str, float] = {}  # assigned, not added: a repeated hour must not double count
            for back in range(days + 1):
                day = end - timedelta(days=back)
                diag["hourly_days_requested"] += 1
                items, error = await self._hourly_items(count, day)
                if items is None:
                    diag["hourly_failures"] += 1
                    diag["last_error"] = error
                    log.warning("Hourly water for meter %s on %s failed: %s", count, day, error)
                    continue
                unit = _raw_unit(items)
                factor = unit_factor(unit)
                if unit and diag["unit"] is None:
                    diag["unit"], diag["unit_factor"] = unit, factor or 1.0
                    diag["unit_unknown"] = unit if factor is None else None
                rows, bad = hourly_rows(items, tz, factor or 1.0)
                if bad:
                    diag["unparsed_items"] += len(bad)
                    diag["bad_stamp"] = diag["bad_stamp"] or str(bad[0])
                if rows:
                    diag["hourly_days_with_data"] += 1
                if items and diag["sample_stamp"] is None:
                    diag["sample_stamp"] = str(_pick(items[0], STAMP_FIELDS))
                day_rows = dict(rows)
                values = [day_rows[k] for k in sorted(day_rows)]
                if looks_cumulative(values):
                    keys = sorted(day_rows)
                    day_rows = {b: day_rows[b] - day_rows[a] for a, b in zip(keys, keys[1:])}
                    diag["cumulative"] = True
                meter_hours.update(day_rows)
                await asyncio.sleep(0.15)
            for hour, amount in meter_hours.items():
                hourly[hour] = hourly.get(hour, 0) + amount
        diag["hourly_url_format"] = "MM-DD-YYYY" if self._hourly_fmt == "mdy" else "YYYY-MM-DD"
        self.diagnostics = diag
        return sorted(daily.items()), sorted(hourly.items())

    def names(self) -> dict[str, str]:
        return {f"water:{m.get('meterCount')}": "Water" for m in self._meters}

    async def status(self) -> dict:
        return {"connected": self.active, "devices": len(self._meters)}

    async def close(self) -> None:
        await self._http.aclose()
