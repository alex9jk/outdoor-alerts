"""Open-Meteo forecast client. Thin: returns the parsed response, no shaping."""

from typing import Any

import httpx

from gooddays.fetch.http import RawCallback, get_json, notify_raw
from gooddays.models import Settings, Spot

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_VARIABLES = (
    "temperature_2m",
    "precipitation_probability",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "snowfall",
    "weather_code",
)
DAILY_VARIABLES = ("sunrise", "sunset")
FORECAST_DAYS = 2


async def fetch_forecast(
    client: httpx.AsyncClient,
    spot: Spot,
    settings: Settings,
    *,
    on_raw: RawCallback | None = None,
) -> dict[str, Any]:
    params: dict[str, str | int] = {
        "latitude": str(spot.lat),
        "longitude": str(spot.lon),
        "hourly": ",".join(HOURLY_VARIABLES),
        "daily": ",".join(DAILY_VARIABLES),
        "forecast_days": FORECAST_DAYS,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": settings.timezone,
        # Without this, times are local labels at ONE fixed UTC offset for the whole
        # response, which is an hour off after a DST change. Unix times are exact.
        "timeformat": "unixtime",
    }
    raw = await get_json(client, "open_meteo", FORECAST_URL, params=params)
    notify_raw(on_raw, spot.id, "open_meteo", raw)
    return raw
