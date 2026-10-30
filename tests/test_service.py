import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from gooddays.fetch.nws import ALERTS_URL
from gooddays.fetch.open_meteo import FORECAST_URL
from gooddays.fetch.service import (
    MAX_CONCURRENT_SPOTS,
    NormalizeError,
    fetch_conditions,
    normalize_alerts,
    normalize_forecast,
)
from gooddays.models import Settings, Spot

FIXTURES = Path(__file__).parent / "fixtures"
TZ = "America/New_York"
ET = ZoneInfo(TZ)
SETTINGS = Settings(nws_user_agent="gooddays-test (t@example.com)", timezone=TZ)


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def make_spot(n: int) -> Spot:
    return Spot(
        id=f"spot-{n}",
        name=f"Spot {n}",
        lat=40.0 + n,
        lon=-75.0,
        activities=("hiking",),
    )


# --- normalize_forecast ---


def test_sunny_day() -> None:
    hourly, daylight = normalize_forecast(fixture("open_meteo_sunny.json"), TZ)

    assert len(hourly) == 3
    first = hourly[0]
    assert first.time == datetime(2026, 6, 15, 9, 0, tzinfo=ET)
    assert first.time.utcoffset() == timedelta(hours=-4)  # EDT
    assert first.temperature_f == 68.0
    assert first.precipitation_probability_pct == 0
    assert first.wind_speed_mph == 4.0
    assert first.wind_gust_mph == 8.0
    assert first.weather_code == 0
    assert [w.date for w in daylight] == [date(2026, 6, 15), date(2026, 6, 16)]
    assert daylight[0].sunrise == datetime(2026, 6, 15, 5, 25, tzinfo=ET)
    assert daylight[0].sunset == datetime(2026, 6, 15, 20, 35, tzinfo=ET)


def test_rainy_day() -> None:
    hourly, _ = normalize_forecast(fixture("open_meteo_rainy.json"), TZ)

    assert [h.precipitation_probability_pct for h in hourly] == [80, 90, 95]
    assert [h.precipitation_in for h in hourly] == [0.05, 0.2, 0.35]
    assert hourly[2].wind_gust_mph == 31.0
    assert hourly[2].weather_code == 65


def test_snow_day() -> None:
    hourly, _ = normalize_forecast(fixture("open_meteo_forecast.json"), TZ)

    assert hourly[0].snowfall_in == 0.0
    assert hourly[1].snowfall_in == 0.1
    assert hourly[1].weather_code == 71  # slight snow fall
    assert hourly[1].time.utcoffset() == timedelta(hours=-5)  # EST in January


def test_missing_fields_become_none() -> None:
    hourly, daylight = normalize_forecast(fixture("open_meteo_missing_fields.json"), TZ)

    assert [h.temperature_f for h in hourly] == [60.0, None, 62.0]  # null values
    assert all(h.precipitation_probability_pct is None for h in hourly)
    assert hourly[2].precipitation_in is None
    assert all(h.wind_gust_mph is None for h in hourly)  # series absent entirely
    assert all(h.snowfall_in is None for h in hourly)
    assert hourly[1].weather_code is None
    # The day with a null sunrise has no daylight window.
    assert [w.date for w in daylight] == [date(2026, 6, 15)]


def test_aware_timestamps_are_kept() -> None:
    raw = fixture("open_meteo_sunny.json")
    raw["hourly"]["time"][0] = "2026-06-15T09:00:00+00:00"

    hourly, _ = normalize_forecast(raw, TZ)

    assert hourly[0].time == datetime(2026, 6, 15, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r.pop("hourly"), "missing 'hourly'"),
        (lambda r: r.pop("daily"), "missing 'daily'"),
        (lambda r: r["hourly"].pop("time"), "missing 'time'"),
        (
            lambda r: r["hourly"]["temperature_2m"].pop(),
            "'temperature_2m' should have 3",
        ),
        (lambda r: r["hourly"].update(time=["not a time"] * 3), "malformed forecast"),
        (lambda r: r["hourly"].update(time=[None] * 3), "bad timestamp"),
        (lambda r: r["hourly"].update(weather_code=["x", 1, 2]), "malformed forecast"),
    ],
)
def test_malformed_forecast(mutate: Any, message: str) -> None:
    raw = fixture("open_meteo_sunny.json")
    mutate(raw)
    with pytest.raises(NormalizeError, match=message):
        normalize_forecast(raw, TZ)


# --- normalize_alerts ---


def test_winter_storm_alert() -> None:
    (alert,) = normalize_alerts(fixture("nws_alerts_winter_storm.json"))

    assert alert.event == "Winter Storm Warning"
    assert alert.severity == "Severe"
    assert alert.headline is not None
    assert alert.headline.startswith("Winter Storm Warning issued")
    assert alert.ends == datetime.fromisoformat("2026-01-16T07:00:00-05:00")
    assert alert.ends is not None
    assert alert.ends.utcoffset() == timedelta(hours=-5)


def test_no_alerts_is_empty_tuple_not_none() -> None:
    assert normalize_alerts(fixture("nws_alerts_none.json")) == ()


def test_alert_with_null_fields() -> None:
    (alert,) = normalize_alerts(fixture("nws_alerts_sparse.json"))

    assert alert.event == "Special Weather Statement"
    assert alert.severity is None
    assert alert.headline is None
    assert alert.ends is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "missing 'features'"),
        ({"features": "nope"}, "missing 'features'"),
        ({"features": [{}]}, "malformed alerts"),
        ({"features": [{"properties": {}}]}, "missing 'event'"),
        (
            {"features": [{"properties": {"event": "X", "ends": "garbage"}}]},
            "malformed alerts",
        ),
    ],
)
def test_malformed_alerts(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(NormalizeError, match=message):
        normalize_alerts(raw)


# --- fetch_conditions ---


def mock_apis(
    *,
    forecast: Any = None,
    alerts: Any = None,
) -> tuple[respx.Route, respx.Route]:
    """Mock both APIs. Each arg is a JSON dict or a callable(request) -> Response."""
    forecast = forecast if forecast is not None else fixture("open_meteo_sunny.json")
    alerts = alerts if alerts is not None else fixture("nws_alerts_none.json")

    def route(url: str, behaviour: Any) -> respx.Route:
        if callable(behaviour):
            return respx.get(url).mock(side_effect=behaviour)
        return respx.get(url).respond(json=behaviour)

    return route(FORECAST_URL, forecast), route(ALERTS_URL, alerts)


def lat_of(request: httpx.Request) -> float:
    if "latitude" in request.url.params:
        return float(request.url.params["latitude"])
    return float(request.url.params["point"].split(",")[0])


@respx.mock
async def test_fetch_all_spots_in_order() -> None:
    mock_apis(alerts=fixture("nws_alerts_winter_storm.json"))
    spots = [make_spot(n) for n in range(3)]

    result = await fetch_conditions(spots, SETTINGS)

    assert result.errors == []
    assert [r.spot for r in result.results] == spots
    first = result.results[0]
    assert len(first.hourly) == 3
    assert len(first.daylight) == 2
    assert first.alerts is not None
    assert first.alerts[0].event == "Winter Storm Warning"
    assert first.fetched_at.utcoffset() is not None


@respx.mock
async def test_no_active_alerts_is_empty_tuple() -> None:
    mock_apis()

    result = await fetch_conditions([make_spot(0)], SETTINGS)

    assert result.results[0].alerts == ()


@respx.mock
async def test_one_failing_spot_does_not_stop_others() -> None:
    bad = make_spot(1)

    def forecast(request: httpx.Request) -> httpx.Response:
        if lat_of(request) == bad.lat:
            return httpx.Response(500)
        return httpx.Response(200, json=fixture("open_meteo_sunny.json"))

    mock_apis(forecast=forecast)
    spots = [make_spot(0), bad, make_spot(2)]

    result = await fetch_conditions(spots, SETTINGS)

    assert [r.spot.id for r in result.results] == ["spot-0", "spot-2"]
    (error,) = result.errors
    assert (error.spot_id, error.source, error.status_code) == (
        "spot-1",
        "open_meteo",
        500,
    )


@respx.mock
async def test_nws_failure_keeps_weather_with_unknown_alerts() -> None:
    mock_apis(alerts=lambda request: httpx.Response(503))

    result = await fetch_conditions([make_spot(0)], SETTINGS)

    (conditions,) = result.results
    assert len(conditions.hourly) == 3
    assert conditions.alerts is None  # unknown, not "no alerts"
    (error,) = result.errors
    assert (error.spot_id, error.source, error.status_code) == ("spot-0", "nws", 503)


@respx.mock
async def test_both_apis_failing_records_both_errors() -> None:
    mock_apis(
        forecast=lambda request: httpx.Response(404),
        alerts=lambda request: httpx.Response(404),
    )

    result = await fetch_conditions([make_spot(0)], SETTINGS)

    assert result.results == []
    assert sorted(e.source for e in result.errors) == ["nws", "open_meteo"]


@respx.mock
async def test_malformed_forecast_is_a_normalize_error() -> None:
    bad = make_spot(1)

    def forecast(request: httpx.Request) -> httpx.Response:
        if lat_of(request) == bad.lat:
            return httpx.Response(200, json={"hourly": {}})
        return httpx.Response(200, json=fixture("open_meteo_sunny.json"))

    mock_apis(forecast=forecast)

    result = await fetch_conditions([make_spot(0), bad], SETTINGS)

    assert [r.spot.id for r in result.results] == ["spot-0"]
    (error,) = result.errors
    assert (error.spot_id, error.source) == ("spot-1", "normalize")
    assert error.message.startswith("forecast:")


@respx.mock
async def test_malformed_alerts_keep_weather_with_unknown_alerts() -> None:
    mock_apis(alerts={"unexpected": True})

    result = await fetch_conditions([make_spot(0)], SETTINGS)

    (conditions,) = result.results
    assert conditions.alerts is None
    (error,) = result.errors
    assert (error.source, error.message.startswith("alerts:")) == ("normalize", True)


@respx.mock
async def test_concurrency_is_capped_by_spot() -> None:
    in_flight = 0
    peak = 0

    def tracked(payload: dict[str, Any]) -> Any:
        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return httpx.Response(200, json=payload)

        return handler

    mock_apis(
        forecast=tracked(fixture("open_meteo_sunny.json")),
        alerts=tracked(fixture("nws_alerts_none.json")),
    )
    spots = [make_spot(n) for n in range(20)]

    result = await fetch_conditions(spots, SETTINGS)

    assert len(result.results) == 20
    # Each spot makes 2 requests, so at most 2 * MAX_CONCURRENT_SPOTS at once...
    assert peak <= 2 * MAX_CONCURRENT_SPOTS
    # ...and it really is concurrent.
    assert peak > 2


async def test_no_spots() -> None:
    result = await fetch_conditions([], SETTINGS)
    assert (result.results, result.errors) == ([], [])
