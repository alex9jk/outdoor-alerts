"""Shared builders for the scoring and message tests.

Two days of hourly rows (2026-01-15 and 2026-01-16, New York time) with daylight
from 07:00 to 17:00, and the thresholds the scoring tests use.
"""

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from gooddays.models import (
    Alert,
    CrossCountrySkiingThresholds,
    DaylightWindow,
    DownhillSkiingThresholds,
    HikingThresholds,
    HourlyConditions,
    Spot,
    SpotConditions,
    Thresholds,
)

TZ = "America/New_York"
ET = ZoneInfo(TZ)
XC_THRESHOLDS = CrossCountrySkiingThresholds(
    min_snow_depth_in=5,
    min_temperature_f=10,
    max_temperature_f=32,
    max_wind_mph=10,
    max_precipitation_probability_pct=30,
    max_rain_in=0,
    min_run_hours=4,
)
THRESHOLDS = Thresholds(
    hiking=HikingThresholds(
        min_temperature_f=45,
        max_precipitation_probability_pct=30,
        max_precipitation_in=0.1,
        min_run_hours=4,
    ),
    downhill_skiing=DownhillSkiingThresholds(
        max_temperature_f=32,
        max_wind_mph=10,
        min_snowfall_in=0,
        min_snow_depth_in=0,
        min_run_hours=4,
    ),
    cross_country_skiing=XC_THRESHOLDS,
)

TODAY = date(2026, 1, 15)
TOMORROW = date(2026, 1, 16)
SUNRISE_HOUR, SUNSET_HOUR = 7, 17  # daylight hours are 07:00 to 16:00 (10 hours)

# A good day for both activities, except skiing needs snow (see ski_day()).
GOOD_HOUR: dict[str, Any] = {
    "temperature_f": 60.0,
    "precipitation_probability_pct": 10,
    "precipitation_in": 0.0,
    "rain_in": 0.0,
    "wind_speed_mph": 5.0,
    "wind_gust_mph": 9.0,
    "snowfall_in": 0.0,
    "snow_depth_in": 0.0,
    "weather_code": 1,
}
COLD_CALM_HOUR: dict[str, Any] = {"temperature_f": 20.0, "wind_speed_mph": 3.0}


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def make_spot(*activities: Any) -> Spot:
    return Spot(
        id="spot-0",
        name="Spot 0",
        lat=40.0,
        lon=-75.0,
        activities=tuple(activities),
    )


def build(
    *,
    base: dict[str, Any] = GOOD_HOUR,
    edits: dict[tuple[date, int], dict[str, Any]] | None = None,
    activities: tuple[str, ...] = ("hiking", "downhill_skiing"),
    alerts: tuple[Alert, ...] | None = (),
    fetched_at: datetime | None = None,
    daylight: bool = True,
    days: tuple[date, ...] = (TODAY, TOMORROW),
) -> SpotConditions:
    """Two days of hourly rows; `edits[(day, hour)]` overrides one hour's fields."""
    edits = edits or {}
    hourly = tuple(
        HourlyConditions(time=at(day, hour), **{**base, **edits.get((day, hour), {})})
        for day in days
        for hour in range(24)
    )
    windows = (
        tuple(
            DaylightWindow(day, at(day, SUNRISE_HOUR), at(day, SUNSET_HOUR))
            for day in days
        )
        if daylight
        else ()
    )
    return SpotConditions(
        spot=make_spot(*activities),
        fetched_at=fetched_at or at(TODAY, 0, 30),
        hourly=hourly,
        daylight=windows,
        alerts=alerts,
    )


def day_edits(
    day: date, hours: range, **fields: Any
) -> dict[tuple[date, int], dict[str, Any]]:
    return {(day, h): dict(fields) for h in hours}
