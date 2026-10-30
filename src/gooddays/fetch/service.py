"""fetch_conditions(): fetch and normalize conditions for each spot.

No scoring or judgment happens here. Normalization is done in pure functions
so it can be tested from saved fixtures alone.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from gooddays.fetch.http import FetchError
from gooddays.fetch.nws import fetch_alerts
from gooddays.fetch.open_meteo import fetch_forecast
from gooddays.models import (
    Alert,
    DaylightWindow,
    FetchResult,
    HourlyConditions,
    Settings,
    Spot,
    SpotConditions,
    SpotError,
)

logger = logging.getLogger(__name__)

# Spots processed at once. Each spot makes 2 requests, so up to 10 are in flight.
MAX_CONCURRENT_SPOTS = 5


class NormalizeError(Exception):
    """A response was fetched but didn't have the shape we expected."""


async def fetch_conditions(spots: Sequence[Spot], settings: Settings) -> FetchResult:
    """Fetch every spot concurrently. One bad spot never fails the run.

    - Open-Meteo fails: the spot is dropped and the error recorded.
    - NWS fails: the spot is kept with `alerts=None` (unknown) and the error recorded.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SPOTS)
    async with httpx.AsyncClient() as client:
        outcomes = await asyncio.gather(
            *(_fetch_spot(client, semaphore, spot, settings) for spot in spots)
        )

    result = FetchResult()
    for conditions, errors in outcomes:
        if conditions is not None:
            result.results.append(conditions)
        result.errors.extend(errors)
    logger.info(
        "fetch_conditions_done spots=%d ok=%d errors=%d",
        len(spots),
        len(result.results),
        len(result.errors),
    )
    return result


async def _fetch_spot(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    spot: Spot,
    settings: Settings,
) -> tuple[SpotConditions | None, list[SpotError]]:
    forecast_raw: dict[str, Any] | BaseException
    alerts_raw: dict[str, Any] | BaseException
    async with semaphore:
        forecast_raw, alerts_raw = await asyncio.gather(
            fetch_forecast(client, spot, settings),
            fetch_alerts(client, spot, settings),
            return_exceptions=True,
        )

    errors: list[SpotError] = []

    if isinstance(alerts_raw, FetchError):
        errors.append(_spot_error(spot, alerts_raw))
    elif isinstance(alerts_raw, BaseException):
        raise alerts_raw  # a bug or cancellation, not a fetch failure

    if isinstance(forecast_raw, FetchError):
        errors.append(_spot_error(spot, forecast_raw))
        return None, errors
    if isinstance(forecast_raw, BaseException):
        raise forecast_raw

    try:
        hourly, daylight = normalize_forecast(forecast_raw, settings.timezone)
    except NormalizeError as e:
        errors.append(SpotError(spot.id, "normalize", f"forecast: {e}"))
        return None, errors

    alerts: tuple[Alert, ...] | None = None
    if isinstance(alerts_raw, dict):
        try:
            alerts = normalize_alerts(alerts_raw)
        except NormalizeError as e:
            errors.append(SpotError(spot.id, "normalize", f"alerts: {e}"))

    conditions = SpotConditions(
        spot=spot,
        fetched_at=datetime.now(ZoneInfo(settings.timezone)),
        hourly=hourly,
        daylight=daylight,
        alerts=alerts,
    )
    logger.info(
        "fetch_spot_done spot=%s hours=%d alerts=%s errors=%d",
        spot.id,
        len(hourly),
        "unknown" if alerts is None else len(alerts),
        len(errors),
    )
    return conditions, errors


def _spot_error(spot: Spot, error: FetchError) -> SpotError:
    logger.warning(
        "spot_error spot=%s source=%s status=%s message=%r",
        spot.id,
        error.source,
        error.status_code,
        error.message,
    )
    return SpotError(spot.id, error.source, error.message, error.status_code)


# --- Normalization (pure functions) ---


def normalize_forecast(
    raw: dict[str, Any], timezone: str
) -> tuple[tuple[HourlyConditions, ...], tuple[DaylightWindow, ...]]:
    """Open-Meteo parallel arrays -> hourly rows and daylight windows.

    Missing or null values become None. Open-Meteo returns local times with no
    offset (we ask for `timezone`), so the zone is attached here. During a DST
    change a local time can be ambiguous; we take the first occurrence.
    """
    tz = ZoneInfo(timezone)
    try:
        hourly = _block(raw, "hourly")
        times = _times(hourly)
        n = len(times)
        series = {
            key: _series(hourly, key, n)
            for key in (
                "temperature_2m",
                "precipitation_probability",
                "precipitation",
                "wind_speed_10m",
                "wind_gusts_10m",
                "snowfall",
                "weather_code",
            )
        }
        rows = tuple(
            HourlyConditions(
                time=_parse_time(times[i], tz),
                temperature_f=_opt(series["temperature_2m"][i], float),
                precipitation_probability_pct=_opt(
                    series["precipitation_probability"][i], int
                ),
                precipitation_in=_opt(series["precipitation"][i], float),
                wind_speed_mph=_opt(series["wind_speed_10m"][i], float),
                wind_gust_mph=_opt(series["wind_gusts_10m"][i], float),
                snowfall_in=_opt(series["snowfall"][i], float),
                weather_code=_opt(series["weather_code"][i], int),
            )
            for i in range(n)
        )

        daily = _block(raw, "daily")
        days = _times(daily)
        sunrises = _series(daily, "sunrise", len(days))
        sunsets = _series(daily, "sunset", len(days))
        # A day with no sunrise or sunset (e.g. polar) has no daylight window.
        windows = tuple(
            DaylightWindow(
                date=date.fromisoformat(day),
                sunrise=_parse_time(sunrise, tz),
                sunset=_parse_time(sunset, tz),
            )
            for day, sunrise, sunset in zip(days, sunrises, sunsets, strict=True)
            if sunrise is not None and sunset is not None
        )
    except NormalizeError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise NormalizeError(f"malformed forecast: {type(e).__name__}: {e}") from e
    return rows, windows


def normalize_alerts(raw: dict[str, Any]) -> tuple[Alert, ...]:
    """NWS GeoJSON features -> alerts. No features means no active alerts."""
    features = raw.get("features")
    if not isinstance(features, list):
        raise NormalizeError("missing 'features' list")
    try:
        alerts: list[Alert] = []
        for feature in features:
            props = feature["properties"]
            event = props.get("event")
            if not isinstance(event, str) or not event:
                raise NormalizeError("alert is missing 'event'")
            ends = props.get("ends")
            alerts.append(
                Alert(
                    event=event,
                    severity=_opt(props.get("severity"), str),
                    headline=_opt(props.get("headline"), str),
                    ends=None if ends is None else datetime.fromisoformat(ends),
                )
            )
    except NormalizeError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise NormalizeError(f"malformed alerts: {type(e).__name__}: {e}") from e
    return tuple(alerts)


def _block(raw: dict[str, Any], key: str) -> dict[str, Any]:
    block = raw.get(key)
    if not isinstance(block, dict):
        raise NormalizeError(f"missing '{key}' object")
    return block


def _times(block: dict[str, Any]) -> list[Any]:
    times = block.get("time")
    if not isinstance(times, list):
        raise NormalizeError("missing 'time' list")
    return times


def _series(block: dict[str, Any], key: str, n: int) -> list[Any]:
    """The values for `key`, or n Nones if the whole series is absent."""
    values = block.get(key)
    if values is None:
        return [None] * n
    if not isinstance(values, list) or len(values) != n:
        raise NormalizeError(f"'{key}' should have {n} values")
    return values


def _opt[T](value: Any, convert: type[T]) -> T | None:
    return None if value is None else convert(value)  # type: ignore[call-arg]


def _parse_time(value: Any, tz: ZoneInfo) -> datetime:
    if not isinstance(value, str):
        raise NormalizeError(f"bad timestamp: {value!r}")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
