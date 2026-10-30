"""Data models shared across the pipeline.

Frozen dataclasses (stdlib only, no new dependencies). Datetimes are
timezone-aware. Weather fields are optional because APIs sometimes omit
values; fetch_conditions normalizes but never fills in or judges them.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

Activity = Literal["hiking", "biking", "skiing"]

# Spot ids end up in log lines and data/raw/{date}/{spot_id}/ paths.
SPOT_ID_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class Settings:
    """The `settings` block of spots.yaml."""

    nws_user_agent: str
    timezone: str  # IANA name, e.g. "America/New_York"


@dataclass(frozen=True, slots=True)
class Spot:
    id: str
    name: str
    lat: float
    lon: float
    activities: tuple[Activity, ...]


@dataclass(frozen=True, slots=True)
class HourlyConditions:
    time: datetime
    temperature_f: float | None
    precipitation_probability_pct: int | None
    precipitation_in: float | None
    wind_speed_mph: float | None
    wind_gust_mph: float | None
    snowfall_in: float | None
    weather_code: int | None  # WMO code, as returned by Open-Meteo


@dataclass(frozen=True, slots=True)
class DaylightWindow:
    date: date
    sunrise: datetime
    sunset: datetime


@dataclass(frozen=True, slots=True)
class Alert:
    event: str  # e.g. "Winter Storm Warning"
    severity: str | None  # NWS: Minor/Moderate/Severe/Extreme/Unknown
    headline: str | None
    ends: datetime | None


@dataclass(frozen=True, slots=True)
class SpotConditions:
    spot: Spot
    fetched_at: datetime
    hourly: tuple[HourlyConditions, ...]
    daylight: tuple[DaylightWindow, ...]  # one per forecast day
    # None means unknown (the NWS fetch failed); () means checked, none active.
    alerts: tuple[Alert, ...] | None


@dataclass(frozen=True, slots=True)
class SpotError:
    spot_id: str
    source: Literal["open_meteo", "nws", "normalize", "unexpected"]
    message: str
    status_code: int | None = None  # set for HTTP errors


@dataclass(frozen=True, slots=True)
class FetchResult:
    results: list[SpotConditions] = field(default_factory=list)
    errors: list[SpotError] = field(default_factory=list)
