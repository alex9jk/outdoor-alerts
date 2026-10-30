import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from gooddays.fetch import service
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
from helpers import load_fixture

TZ = "America/New_York"
ET = ZoneInfo(TZ)
SETTINGS = Settings(nws_user_agent="gooddays-test (t@example.com)", timezone=TZ)


def make_spot(n: int) -> Spot:
    return Spot(
        id=f"spot-{n}",
        name=f"Spot {n}",
        lat=40.0 + n,
        lon=-75.0,
        activities=("hiking",),
    )


def unix(*args: int) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


# --- normalize_forecast ---


def test_sunny_day() -> None:
    hourly, daylight = normalize_forecast(load_fixture("open_meteo_sunny.json"), TZ)

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
    hourly, _ = normalize_forecast(load_fixture("open_meteo_rainy.json"), TZ)

    assert [h.precipitation_probability_pct for h in hourly] == [80, 90, 95]
    assert [h.precipitation_in for h in hourly] == [0.05, 0.2, 0.35]
    assert hourly[2].wind_gust_mph == 31.0
    assert hourly[2].weather_code == 65


def test_snow_day() -> None:
    hourly, _ = normalize_forecast(load_fixture("open_meteo_forecast.json"), TZ)

    assert hourly[0].snowfall_in == 0.0
    assert hourly[1].snowfall_in == 0.1
    assert hourly[1].weather_code == 71  # slight snow fall
    assert hourly[1].time.utcoffset() == timedelta(hours=-5)  # EST in January


def test_missing_fields_become_none() -> None:
    hourly, daylight = normalize_forecast(
        load_fixture("open_meteo_missing_fields.json"), TZ
    )

    assert [h.temperature_f for h in hourly] == [60.0, None, 62.0]  # null values
    assert all(h.precipitation_probability_pct is None for h in hourly)
    assert hourly[2].precipitation_in is None
    assert all(h.wind_gust_mph is None for h in hourly)  # series absent entirely
    assert all(h.snowfall_in is None for h in hourly)
    assert hourly[1].weather_code is None
    # The day with a null sunrise has no daylight window.
    assert [w.date for w in daylight] == [date(2026, 6, 15)]


def test_fall_back_day_from_a_real_response() -> None:
    """2026-11-01: 01:00 happens twice. Open-Meteo's own iso labels drift by an
    hour after the change, which is why we request unix times."""
    hourly, daylight = normalize_forecast(
        load_fixture("open_meteo_dst_fall_back.json"), TZ
    )

    utc = [h.time.astimezone(UTC) for h in hourly]
    assert len(hourly) == 48
    assert all(b - a == timedelta(hours=1) for a, b in zip(utc, utc[1:], strict=False))
    # The repeated hour: first 01:00 EDT, then 01:00 EST.
    assert (hourly[1].time.hour, hourly[1].time.utcoffset()) == (1, timedelta(hours=-4))
    assert (hourly[2].time.hour, hourly[2].time.utcoffset()) == (1, timedelta(hours=-5))
    assert hourly[2].time.astimezone(UTC) - hourly[1].time.astimezone(UTC) == timedelta(
        hours=1
    )
    # Real local sunrise on 2026-11-01 was 06:43 EST (the iso label said 07:43).
    assert [w.date for w in daylight] == [date(2026, 11, 1), date(2026, 11, 2)]
    assert (daylight[0].sunrise.hour, daylight[0].sunrise.minute) == (6, 43)
    assert daylight[0].sunrise.utcoffset() == timedelta(hours=-5)


def test_spring_forward_skips_the_missing_hour() -> None:
    raw = load_fixture("open_meteo_sunny.json")
    # 2026-03-08: 01:00 EST (06:00Z), then 03:00 EDT (07:00Z). 02:00 doesn't exist.
    raw["hourly"]["time"] = [
        unix(2026, 3, 8, 6),
        unix(2026, 3, 8, 7),
        unix(2026, 3, 8, 8),
    ]

    hourly, _ = normalize_forecast(raw, TZ)

    assert [h.time.hour for h in hourly] == [1, 3, 4]
    assert [h.time.utcoffset() for h in hourly] == [
        timedelta(hours=-5),
        timedelta(hours=-4),
        timedelta(hours=-4),
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r.pop("hourly"), "missing 'hourly'"),
        (lambda r: r.pop("daily"), "missing 'daily'"),
        (lambda r: r.pop("utc_offset_seconds"), "utc_offset_seconds"),
        (lambda r: r["hourly"].pop("time"), "missing 'time'"),
        (
            lambda r: r["hourly"]["temperature_2m"].pop(),
            "'temperature_2m' should have 3",
        ),
        (lambda r: r["hourly"].update(time=["2026-06-15T09:00"] * 3), "bad timestamp"),
        (lambda r: r["hourly"].update(time=[None] * 3), "bad timestamp"),
        (lambda r: r["hourly"].update(time=[True] * 3), "bad timestamp"),
        (lambda r: r["hourly"].update(time=[1e300] * 3), "malformed forecast"),
        (lambda r: r["hourly"].update(weather_code=["x", 1, 2]), "not a finite number"),
        (
            lambda r: r["hourly"].update(weather_code=[True, 1, 2]),
            "not a finite number",
        ),
        (
            lambda r: r["hourly"].update(precipitation_probability=[52.7, 0, 0]),
            "whole number",
        ),
        (
            lambda r: r["hourly"].update(temperature_2m=[float("inf"), 1.0, 2.0]),
            "finite",
        ),
        (
            lambda r: r["hourly"].update(temperature_2m=[float("nan"), 1.0, 2.0]),
            "finite",
        ),
        (lambda r: r["daily"].update(sunrise="nope"), "'sunrise' should have 2"),
        (lambda r: r.update(utc_offset_seconds=10**9), "malformed forecast"),
    ],
)
def test_malformed_forecast(mutate: Any, message: str) -> None:
    raw = load_fixture("open_meteo_sunny.json")
    mutate(raw)
    with pytest.raises(NormalizeError, match=message):
        normalize_forecast(raw, TZ)


def test_whole_number_floats_are_accepted_as_ints() -> None:
    raw = load_fixture("open_meteo_sunny.json")
    raw["hourly"]["precipitation_probability"] = [52.0, 0, 0]

    hourly, _ = normalize_forecast(raw, TZ)

    assert hourly[0].precipitation_probability_pct == 52


def test_invalid_timezone_is_a_normalize_error() -> None:
    with pytest.raises(NormalizeError, match="malformed forecast"):
        normalize_forecast(load_fixture("open_meteo_sunny.json"), "Not/AZone")


# --- normalize_alerts ---


def test_winter_storm_alert() -> None:
    (alert,) = normalize_alerts(load_fixture("nws_alerts_winter_storm.json"))

    assert alert.event == "Winter Storm Warning"
    assert alert.severity == "Severe"
    assert alert.headline is not None
    assert alert.headline.startswith("Winter Storm Warning issued")
    assert alert.ends == datetime.fromisoformat("2026-01-16T07:00:00-05:00")
    assert alert.ends is not None
    assert alert.ends.utcoffset() == timedelta(hours=-5)


def test_no_alerts_is_empty_tuple_not_none() -> None:
    assert normalize_alerts(load_fixture("nws_alerts_none.json")) == ()


def test_alert_with_null_fields() -> None:
    (alert,) = normalize_alerts(load_fixture("nws_alerts_sparse.json"))

    assert alert.event == "Special Weather Statement"
    assert alert.severity is None
    assert alert.headline is None
    assert alert.ends is None


def alert_with(**props: Any) -> dict[str, Any]:
    return {"features": [{"properties": {"event": "X", **props}}]}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "missing 'features'"),
        ({"features": "nope"}, "missing 'features'"),
        ({"features": [{}]}, "no 'properties' object"),
        ({"features": [None]}, "no 'properties' object"),
        ({"features": ["text"]}, "no 'properties' object"),
        ({"features": [{"properties": None}]}, "no 'properties' object"),
        ({"features": [{"properties": "text"}]}, "no 'properties' object"),
        ({"features": [{"properties": {}}]}, "missing 'event'"),
        ({"features": [{"properties": {"event": ""}}]}, "missing 'event'"),
        (alert_with(ends="garbage"), "malformed alerts"),
        (alert_with(ends=12345), "malformed alerts"),
        (alert_with(ends="2026-01-16T07:00:00"), "no UTC offset"),
        (alert_with(severity=3), "malformed alerts"),
    ],
)
def test_malformed_alerts(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(NormalizeError, match=message):
        normalize_alerts(raw)


# --- fetch_conditions ---


def mock_apis(
    *, forecast: Any = None, alerts: Any = None
) -> tuple[respx.Route, respx.Route]:
    """Mock both APIs. Each arg is a JSON dict or a callable(request) -> Response."""
    forecast = (
        forecast if forecast is not None else load_fixture("open_meteo_sunny.json")
    )
    alerts = alerts if alerts is not None else load_fixture("nws_alerts_none.json")

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
    mock_apis(alerts=load_fixture("nws_alerts_winter_storm.json"))
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
async def test_no_spots() -> None:
    result = await fetch_conditions([], SETTINGS)
    assert (result.results, result.errors) == ([], [])


# One spot's failure must never stop the others.


@respx.mock
async def test_open_meteo_http_error_drops_only_that_spot() -> None:
    bad = make_spot(1)

    def forecast(request: httpx.Request) -> httpx.Response:
        if lat_of(request) == bad.lat:
            return httpx.Response(500)
        return httpx.Response(200, json=load_fixture("open_meteo_sunny.json"))

    mock_apis(forecast=forecast)

    result = await fetch_conditions([make_spot(0), bad, make_spot(2)], SETTINGS)

    assert [r.spot.id for r in result.results] == ["spot-0", "spot-2"]
    (error,) = result.errors
    assert (error.spot_id, error.source, error.status_code) == (
        "spot-1",
        "open_meteo",
        500,
    )


@respx.mock
async def test_open_meteo_timeout_is_retried_then_recorded() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    forecast_route, _ = mock_apis(forecast=slow)

    result = await fetch_conditions([make_spot(0)], SETTINGS)

    assert result.results == []
    (error,) = result.errors
    assert (error.spot_id, error.source, error.status_code) == (
        "spot-0",
        "open_meteo",
        None,
    )
    assert error.message.startswith("timeout")
    assert forecast_route.call_count == 3


@respx.mock
async def test_open_meteo_connection_error_is_recorded() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    mock_apis(forecast=refuse)

    result = await fetch_conditions([make_spot(0)], SETTINGS)

    assert result.results == []
    (error,) = result.errors
    assert (error.source, error.message.startswith("request failed")) == (
        "open_meteo",
        True,
    )


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
        return httpx.Response(200, json=load_fixture("open_meteo_sunny.json"))

    mock_apis(forecast=forecast)

    result = await fetch_conditions([make_spot(0), bad], SETTINGS)

    assert [r.spot.id for r in result.results] == ["spot-0"]
    (error,) = result.errors
    assert (error.spot_id, error.source) == ("spot-1", "normalize")
    assert error.message.startswith("forecast:")


@respx.mock
async def test_unexpected_exception_from_a_client_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_apis()
    real = service.fetch_forecast

    async def flaky(client: httpx.AsyncClient, spot: Spot, settings: Settings) -> Any:
        if spot.id == "spot-1":
            raise RuntimeError("boom")
        return await real(client, spot, settings)

    monkeypatch.setattr(service, "fetch_forecast", flaky)

    result = await fetch_conditions(
        [make_spot(0), make_spot(1), make_spot(2)], SETTINGS
    )

    assert [r.spot.id for r in result.results] == ["spot-0", "spot-2"]
    (error,) = result.errors
    assert (error.spot_id, error.source) == ("spot-1", "open_meteo")
    assert error.message == "unexpected RuntimeError: boom"


@respx.mock
async def test_unexpected_exception_during_normalization_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_apis()
    real = service.normalize_forecast
    calls = 0

    def flaky(raw: dict[str, Any], timezone_name: str) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("boom")
        return real(raw, timezone_name)

    monkeypatch.setattr(service, "normalize_forecast", flaky)

    result = await fetch_conditions([make_spot(n) for n in range(4)], SETTINGS)

    assert len(result.results) == 3
    (error,) = result.errors
    assert error.source == "unexpected"
    assert error.message == "RuntimeError: boom"


@respx.mock
async def test_cancellation_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_apis()

    async def cancelled(*args: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "fetch_forecast", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await fetch_conditions([make_spot(0)], SETTINGS)


# NWS failures keep the weather and mark alerts as unknown (None).


@respx.mock
async def test_nws_http_error_keeps_weather_with_unknown_alerts() -> None:
    mock_apis(alerts=lambda request: httpx.Response(503))

    result = await fetch_conditions([make_spot(0)], SETTINGS)

    (conditions,) = result.results
    assert len(conditions.hourly) == 3
    assert conditions.alerts is None  # unknown, not "no alerts"
    (error,) = result.errors
    assert (error.spot_id, error.source, error.status_code) == ("spot-0", "nws", 503)


@respx.mock
async def test_nws_timeout_keeps_weather_with_unknown_alerts() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    mock_apis(alerts=slow)

    result = await fetch_conditions([make_spot(0)], SETTINGS)

    (conditions,) = result.results
    assert conditions.alerts is None
    (error,) = result.errors
    assert (error.source, error.status_code) == ("nws", None)
    assert error.message.startswith("timeout")


@respx.mock
async def test_unexpected_exception_from_nws_client_keeps_weather(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_apis()

    async def bad_header(*args: Any) -> Any:
        raise UnicodeEncodeError("ascii", "é", 0, 1, "ordinal not in range")

    monkeypatch.setattr(service, "fetch_alerts", bad_header)

    result = await fetch_conditions([make_spot(0)], SETTINGS)

    (conditions,) = result.results
    assert conditions.alerts is None
    (error,) = result.errors
    assert (
        error.source,
        error.message.startswith("unexpected UnicodeEncodeError"),
    ) == (
        "nws",
        True,
    )


@respx.mock
@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": True},
        {"features": [{"properties": None}]},
        {"features": [{"properties": {"event": "X", "ends": "2026-01-16T07:00:00"}}]},
    ],
)
async def test_malformed_alerts_keep_weather_with_unknown_alerts(
    payload: dict[str, Any],
) -> None:
    mock_apis(alerts=payload)

    result = await fetch_conditions([make_spot(0)], SETTINGS)

    (conditions,) = result.results
    assert conditions.alerts is None
    (error,) = result.errors
    assert error.source == "normalize"
    assert error.message.startswith("alerts:")


# Concurrency and logging.


@respx.mock
async def test_concurrency_is_capped_by_spot() -> None:
    in_flight = 0
    peak = 0

    def tracked(payload: dict[str, Any]) -> Any:
        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return httpx.Response(200, json=payload)

        return handler

    mock_apis(
        forecast=tracked(load_fixture("open_meteo_sunny.json")),
        alerts=tracked(load_fixture("nws_alerts_none.json")),
    )

    result = await fetch_conditions([make_spot(n) for n in range(20)], SETTINGS)

    assert len(result.results) == 20
    # Each spot makes 2 requests, and exactly MAX_CONCURRENT_SPOTS spots run at once.
    assert peak == 2 * MAX_CONCURRENT_SPOTS


@respx.mock
async def test_success_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    mock_apis()

    with caplog.at_level(logging.INFO, logger="gooddays.fetch.service"):
        await fetch_conditions([make_spot(0)], SETTINGS)

    assert "fetch_spot_done spot=spot-0 hours=3 alerts=0 errors=0" in caplog.text
    assert "fetch_conditions_done spots=1 ok=1 errors=0" in caplog.text


@respx.mock
async def test_failures_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    mock_apis(
        forecast=lambda request: httpx.Response(404),
        alerts=lambda request: httpx.Response(503),
    )

    with caplog.at_level(logging.INFO, logger="gooddays.fetch.service"):
        await fetch_conditions([make_spot(0)], SETTINGS)

    assert "spot_error spot=spot-0 source=open_meteo status=404" in caplog.text
    assert "spot_error spot=spot-0 source=nws status=503" in caplog.text
    assert "fetch_conditions_done spots=1 ok=0 errors=2" in caplog.text


@respx.mock
async def test_unexpected_errors_are_logged(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_apis()

    def boom(raw: dict[str, Any], timezone_name: str) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "normalize_forecast", boom)

    with caplog.at_level(logging.INFO, logger="gooddays.fetch.service"):
        await fetch_conditions([make_spot(0)], SETTINGS)

    assert "spot_unexpected spot=spot-0 error=RuntimeError: boom" in caplog.text
