"""Data models shared across the pipeline.

Frozen dataclasses (stdlib only, no new dependencies). Datetimes are
timezone-aware. Weather fields are optional because APIs sometimes omit
values; fetch_conditions normalizes but never fills in or judges them.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

Activity = Literal["hiking", "biking", "downhill_skiing", "cross_country_skiing"]

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
    precipitation_in: float | None  # rain plus melted snow
    rain_in: float | None  # liquid rain only (not snow, not snow showers)
    wind_speed_mph: float | None
    wind_gust_mph: float | None
    snowfall_in: float | None
    snow_depth_in: float | None  # snow on the ground, in inches
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


# --- Scoring: thresholds (from spots.yaml) and results ---


@dataclass(frozen=True, slots=True)
class HikingThresholds:
    min_temperature_f: float  # an hour passes at or above this
    max_precipitation_probability_pct: float  # at or below this
    max_precipitation_in: float  # per hour, at or below this
    min_run_hours: int  # consecutive passing daylight hours needed


@dataclass(frozen=True, slots=True)
class DownhillSkiingThresholds:
    max_temperature_f: float  # an hour passes below this
    max_wind_mph: float  # below this
    min_snowfall_in: float  # total overnight snowfall counts as snow above this
    min_snow_depth_in: float  # snow on the ground counts above this
    min_run_hours: int  # consecutive passing daylight hours needed


@dataclass(frozen=True, slots=True)
class CrossCountrySkiingThresholds:
    min_snow_depth_in: float  # an hour passes at or above this depth on the ground
    min_temperature_f: float  # at or above this ...
    max_temperature_f: float  # ... and below this
    max_wind_mph: float  # below this
    max_precipitation_probability_pct: float  # at or below this
    max_rain_in: float  # liquid rain per hour, at or below this (0 = no rain)
    min_run_hours: int  # consecutive passing daylight hours needed


@dataclass(frozen=True, slots=True)
class Thresholds:
    hiking: HikingThresholds
    downhill_skiing: DownhillSkiingThresholds
    cross_country_skiing: CrossCountrySkiingThresholds


ScoreStatus = Literal["good", "not_good", "no_rules"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    value: str  # what was found, in words
    limit: str  # what the check needs


@dataclass(frozen=True, slots=True)
class ActivityScore:
    activity: Activity
    day: date
    status: ScoreStatus
    checks: tuple[CheckResult, ...]
    reasons: tuple[str, ...]  # one per failed check, plus things like "no data"
    notes: tuple[str, ...]  # extra information that doesn't change the status


@dataclass(frozen=True, slots=True)
class SpotScore:
    spot: Spot
    fetched_at: datetime
    scores: tuple[ActivityScore, ...]


@dataclass(frozen=True, slots=True)
class ScoreResult:
    results: tuple[SpotScore, ...]


# --- Messages (generate_message) ---


@dataclass(frozen=True, slots=True)
class GoodEntry:
    """One good spot/activity/day, with the numbers a message can mention."""

    spot_name: str  # ASCII-only, so it can go straight into an SMS
    activity: Activity
    day: date
    day_label: Literal["today", "tomorrow"]
    temperature_low_f: float | None
    temperature_high_f: float | None
    wind_max_mph: float | None
    sunrise: datetime | None
    sunset: datetime | None
    snow_depth_in: float | None  # only set for the skiing activities
    alert_notes: tuple[str, ...]  # non-severe alerts still active


@dataclass(frozen=True, slots=True)
class MessageFacts:
    entries: tuple[GoodEntry, ...]
    unchecked_spots: int  # spots with any fetch error


@dataclass(frozen=True, slots=True)
class Message:
    body: str  # SMS text: plain ASCII, no subject
    source: Literal["ai", "fallback"]


# --- Sending (send_notification) ---


@dataclass(frozen=True, slots=True)
class SendResult:
    """What happened when we tried to send. Nothing is stored."""

    accepted: bool  # the provider accepted it (not "delivered to the phone")
    message_id: str | None
    attempts: int  # requests actually made
    reason: str | None  # a short key when nothing was accepted
