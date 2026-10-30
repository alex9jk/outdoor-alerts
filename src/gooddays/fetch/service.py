"""fetch_conditions(): fetch and normalize conditions for each spot.

No scoring or judgment happens here. Normalization is done in pure functions
so it can be tested from saved fixtures alone.
"""

import asyncio
import logging
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from gooddays.fetch.http import FetchError, Source
from gooddays.fetch.nws import fetch_alerts
from gooddays.fetch.open_meteo import HOURLY_VARIABLES, fetch_forecast
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

# What malformed response data can raise while it is being read and converted.
_PARSE_ERRORS = (
    KeyError,
    TypeError,
    ValueError,
    AttributeError,
    OverflowError,
    OSError,
)


class NormalizeError(Exception):
    """A response was fetched but didn't have the shape we expected."""


async def fetch_conditions(spots: Sequence[Spot], settings: Settings) -> FetchResult:
    """Fetch every spot concurrently. One bad spot never fails the run.

    - Open-Meteo fails: the spot is dropped and the error recorded.
    - NWS fails: the spot is kept with `alerts=None` (unknown) and the error recorded.
    - Anything unexpected inside one spot is recorded for that spot only.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SPOTS)
    async with httpx.AsyncClient() as client:
        outcomes = await asyncio.gather(
            *(_fetch_spot(client, semaphore, spot, settings) for spot in spots)
        )

    result = FetchResult(
        results=[conditions for conditions, _ in outcomes if conditions is not None],
        errors=[error for _, errors in outcomes for error in errors],
    )
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
    try:
        return await _fetch_spot_unguarded(client, semaphore, spot, settings)
    except Exception as e:  # last resort so one spot can't take down the run
        # CancelledError is a BaseException, so cancellation still propagates.
        logger.error(
            "spot_unexpected spot=%s error=%s: %s",
            spot.id,
            type(e).__name__,
            e,
            exc_info=True,
        )
        return None, [SpotError(spot.id, "unexpected", f"{type(e).__name__}: {e}")]


async def _fetch_spot_unguarded(
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

    errors = [
        error
        for error in (
            _failure(spot, "open_meteo", forecast_raw),
            _failure(spot, "nws", alerts_raw),
        )
        if error is not None
    ]

    if not isinstance(forecast_raw, dict):
        return None, errors  # no forecast, no spot
    try:
        hourly, daylight = normalize_forecast(forecast_raw, settings.timezone)
    except NormalizeError as e:
        errors.append(_normalize_error(spot, "forecast", e))
        return None, errors

    alerts: tuple[Alert, ...] | None = None  # None: unknown
    if isinstance(alerts_raw, dict):
        try:
            alerts = normalize_alerts(alerts_raw)
        except NormalizeError as e:
            errors.append(_normalize_error(spot, "alerts", e))

    conditions = SpotConditions(
        spot=spot,
        fetched_at=datetime.now(UTC),
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


def _failure(
    spot: Spot, source: Source, outcome: dict[str, Any] | BaseException
) -> SpotError | None:
    """The SpotError for a failed request, or None if it succeeded."""
    if isinstance(outcome, dict):
        return None
    if isinstance(outcome, FetchError):
        logger.warning(
            "spot_error spot=%s source=%s status=%s message=%r",
            spot.id,
            outcome.source,
            outcome.status_code,
            outcome.message,
        )
        return SpotError(spot.id, outcome.source, outcome.message, outcome.status_code)
    if isinstance(outcome, Exception):
        # Not a fetch failure we know about (e.g. a request that can't be built).
        message = f"unexpected {type(outcome).__name__}: {outcome}"
        logger.warning(
            "spot_error spot=%s source=%s status=None message=%r",
            spot.id,
            source,
            message,
        )
        return SpotError(spot.id, source, message)
    raise outcome  # cancellation and the like must not be swallowed


def _normalize_error(spot: Spot, what: str, error: NormalizeError) -> SpotError:
    logger.warning(
        "spot_error spot=%s source=normalize what=%s message=%r",
        spot.id,
        what,
        str(error),
    )
    return SpotError(spot.id, "normalize", f"{what}: {error}")


# --- Normalization (pure functions) ---


def normalize_forecast(
    raw: dict[str, Any], timezone_name: str
) -> tuple[tuple[HourlyConditions, ...], tuple[DaylightWindow, ...]]:
    """Open-Meteo parallel arrays -> hourly rows and daylight windows.

    Expects `timeformat=unixtime`, so every time is an exact instant that we
    express in the spot's zone. Missing or null values become None.
    """
    try:
        tz = ZoneInfo(timezone_name)
        hourly = _block(raw, "hourly")
        times = _times(hourly)
        n = len(times)
        series = {key: _series(hourly, key, n) for key in HOURLY_VARIABLES}
        rows = tuple(
            HourlyConditions(
                time=_instant(times[i], tz),
                temperature_f=_opt(series["temperature_2m"][i], _float),
                precipitation_probability_pct=_opt(
                    series["precipitation_probability"][i], _int
                ),
                precipitation_in=_opt(series["precipitation"][i], _float),
                wind_speed_mph=_opt(series["wind_speed_10m"][i], _float),
                wind_gust_mph=_opt(series["wind_gusts_10m"][i], _float),
                snowfall_in=_opt(series["snowfall"][i], _float),
                weather_code=_opt(series["weather_code"][i], _int),
            )
            for i in range(n)
        )

        # Open-Meteo labels each day at the response's fixed UTC offset, so a day's
        # date comes from that offset, not from the zone's offset at that instant.
        offset = raw.get("utc_offset_seconds")
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise NormalizeError("missing 'utc_offset_seconds'")
        label_tz = timezone(timedelta(seconds=offset))

        daily = _block(raw, "daily")
        days = _times(daily)
        sunrises = _series(daily, "sunrise", len(days))
        sunsets = _series(daily, "sunset", len(days))
        # A day with no sunrise or sunset (e.g. polar) has no daylight window.
        windows = tuple(
            DaylightWindow(
                date=_instant(day, label_tz).date(),
                sunrise=_instant(sunrise, tz),
                sunset=_instant(sunset, tz),
            )
            for day, sunrise, sunset in zip(days, sunrises, sunsets, strict=True)
            if sunrise is not None and sunset is not None
        )
    except NormalizeError:
        raise
    except _PARSE_ERRORS as e:
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
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(props, dict):
                raise NormalizeError("alert has no 'properties' object")
            event = props.get("event")
            if not isinstance(event, str) or not event:
                raise NormalizeError("alert is missing 'event'")
            alerts.append(
                Alert(
                    event=event,
                    severity=_opt(props.get("severity"), _str),
                    headline=_opt(props.get("headline"), _str),
                    ends=_opt(props.get("ends"), _offset_aware_time),
                )
            )
    except NormalizeError:
        raise
    except _PARSE_ERRORS as e:
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


def _opt[T](value: Any, convert: Callable[[Any], T]) -> T | None:
    return None if value is None else convert(value)


def _float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"not a finite number: {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"not a finite number: {value!r}")
    return float(value)


def _int(value: Any) -> int:
    number = _float(value)
    if not number.is_integer():
        raise ValueError(f"not a whole number: {value!r}")
    return int(number)


def _str(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"not a string: {value!r}")
    return value


def _instant(value: Any, tz: timezone | ZoneInfo) -> datetime:
    """A unix timestamp as an aware datetime in `tz`."""
    try:
        seconds = _float(value)
    except ValueError:
        raise NormalizeError(f"bad timestamp: {value!r}") from None
    return datetime.fromtimestamp(seconds, tz)


def _offset_aware_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(_str(value))
    if parsed.tzinfo is None:
        raise NormalizeError(f"alert time has no UTC offset: {value!r}")
    return parsed
