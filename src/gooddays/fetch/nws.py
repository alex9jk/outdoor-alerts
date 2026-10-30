"""NWS active-alerts client. Thin: returns the parsed response, no shaping."""

from typing import Any

import httpx

from gooddays.fetch.http import RawCallback, get_json, notify_raw
from gooddays.models import Settings, Spot

ALERTS_URL = "https://api.weather.gov/alerts/active"


async def fetch_alerts(
    client: httpx.AsyncClient,
    spot: Spot,
    settings: Settings,
    *,
    on_raw: RawCallback | None = None,
) -> dict[str, Any]:
    # Query string is built by hand so the comma in point=lat,lon isn't
    # percent-encoded.
    url = f"{ALERTS_URL}?point={spot.lat},{spot.lon}"
    headers = {
        "User-Agent": settings.nws_user_agent,  # NWS requires a descriptive one
        "Accept": "application/geo+json",
    }
    raw = await get_json(client, "nws", url, headers=headers)
    notify_raw(on_raw, spot.id, "nws", raw)
    return raw
