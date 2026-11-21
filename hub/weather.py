"""Daily weather from Open-Meteo (open-meteo.com): free, no key, no account.

Rain, reference evapotranspiration (FAO-56 ET0, the water a lawn loses to
sun, wind and heat) and the daily high, for the last 92 days and the next 3.
"""

from __future__ import annotations

from datetime import date

import httpx

from .models import HubError

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


async def geocode(query: str, client: httpx.AsyncClient | None = None) -> dict:
    """'McKinney, TX' or a city name -> {"name", "latitude", "longitude", "timezone"}."""
    name, _, region = query.partition(",")
    owns = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        r = await client.get(GEOCODE_URL, params={"name": name.strip(), "count": 10, "countryCode": "US"})
        results = (r.json() if r.status_code == 200 else {}).get("results") or []
    except httpx.HTTPError as exc:
        raise HubError(504, f"Couldn't reach the weather service: {exc}") from exc
    finally:
        if owns:
            await client.aclose()
    region = region.strip().lower()
    states = {"tx": "texas"}
    if region:
        wanted = states.get(region, region)
        results = [x for x in results if (x.get("admin1") or "").lower().startswith(wanted)] or results
    if not results:
        raise HubError(404, f"Couldn't find {query!r}. Try 'City, State'.")
    best = results[0]
    return {"name": f"{best['name']}, {best.get('admin1', '')}".strip(", "), "latitude": best["latitude"],
            "longitude": best["longitude"], "timezone": best.get("timezone", "auto")}


async def daily(latitude: float, longitude: float, client: httpx.AsyncClient | None = None,
                past_days: int = 92) -> list[dict]:
    params = {
        "latitude": latitude, "longitude": longitude, "timezone": "auto",
        "daily": "precipitation_sum,et0_fao_evapotranspiration,temperature_2m_max",
        "past_days": past_days, "forecast_days": 3, "precipitation_unit": "inch", "temperature_unit": "fahrenheit",
    }
    owns = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        r = await client.get(FORECAST_URL, params=params)
        r.raise_for_status()
        data = r.json()["daily"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise HubError(504, f"Weather service: {exc}") from exc
    finally:
        if owns:
            await client.aclose()
    today = date.today().isoformat()
    return [{"day": d, "rain_in": rain, "et0_in": et0, "tmax_f": tmax, "forecast": int(d > today)}
            for d, rain, et0, tmax in zip(data["time"], data["precipitation_sum"],
                                          data["et0_fao_evapotranspiration"], data["temperature_2m_max"])]
