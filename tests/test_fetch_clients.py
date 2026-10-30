import logging
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from gooddays.fetch.http import MAX_ATTEMPTS, FetchError
from gooddays.fetch.nws import ALERTS_URL, fetch_alerts
from gooddays.fetch.open_meteo import FORECAST_URL, fetch_forecast
from gooddays.models import Settings, Spot
from helpers import load_fixture

SPOT = Spot(
    id="bristol", name="Bristol", lat=42.745, lon=-77.404, activities=("skiing",)
)
SETTINGS = Settings(
    nws_user_agent="gooddays-test (t@example.com)", timezone="America/New_York"
)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


# --- Open-Meteo ---


@respx.mock
async def test_forecast_sends_expected_params(client: httpx.AsyncClient) -> None:
    route = respx.get(FORECAST_URL).respond(
        json=load_fixture("open_meteo_forecast.json")
    )

    result = await fetch_forecast(client, SPOT, SETTINGS)

    assert result == load_fixture("open_meteo_forecast.json")
    params = route.calls.last.request.url.params
    assert params["latitude"] == "42.745"
    assert params["longitude"] == "-77.404"
    assert params["hourly"] == (
        "temperature_2m,precipitation_probability,precipitation,"
        "wind_speed_10m,wind_gusts_10m,snowfall,weather_code"
    )
    assert params["daily"] == "sunrise,sunset"
    assert params["forecast_days"] == "2"
    assert params["temperature_unit"] == "fahrenheit"
    assert params["wind_speed_unit"] == "mph"
    assert params["precipitation_unit"] == "inch"
    assert params["timezone"] == "America/New_York"
    assert params["timeformat"] == "unixtime"


# --- NWS ---


@respx.mock
async def test_alerts_sends_point_and_headers(client: httpx.AsyncClient) -> None:
    route = respx.get(ALERTS_URL).respond(
        json=load_fixture("nws_alerts_winter_storm.json")
    )

    result = await fetch_alerts(client, SPOT, SETTINGS)

    assert result == load_fixture("nws_alerts_winter_storm.json")
    request = route.calls.last.request
    assert request.url.params["point"] == "42.745,-77.404"
    assert "point=42.745,-77.404" in str(request.url)  # comma not percent-encoded
    assert request.headers["User-Agent"] == SETTINGS.nws_user_agent
    assert request.headers["Accept"] == "application/geo+json"


@respx.mock
async def test_alerts_empty_feature_collection(client: httpx.AsyncClient) -> None:
    respx.get(ALERTS_URL).respond(json=load_fixture("nws_alerts_none.json"))
    assert (await fetch_alerts(client, SPOT, SETTINGS))["features"] == []


# --- Retry policy (shared helper, exercised through a client) ---


@respx.mock
async def test_timeout_then_success_retries(
    client: httpx.AsyncClient, no_sleep: list[float]
) -> None:
    route = respx.get(FORECAST_URL).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"ok": 1})]
    )

    assert await fetch_forecast(client, SPOT, SETTINGS) == {"ok": 1}
    assert route.call_count == 2
    assert no_sleep == [0.5]


@respx.mock
async def test_server_error_three_times_raises(
    client: httpx.AsyncClient, no_sleep: list[float]
) -> None:
    route = respx.get(FORECAST_URL).respond(500)

    with pytest.raises(FetchError) as exc:
        await fetch_forecast(client, SPOT, SETTINGS)

    assert exc.value.source == "open_meteo"
    assert exc.value.status_code == 500
    assert route.call_count == MAX_ATTEMPTS
    assert no_sleep == [0.5, 1.0]  # exponential, no sleep after the last attempt


@respx.mock
async def test_timeouts_exhaust_retries(client: httpx.AsyncClient) -> None:
    route = respx.get(ALERTS_URL).mock(side_effect=httpx.ConnectTimeout("slow"))

    with pytest.raises(FetchError, match="timeout") as exc:
        await fetch_alerts(client, SPOT, SETTINGS)

    assert exc.value.status_code is None
    assert exc.value.source == "nws"
    assert route.call_count == MAX_ATTEMPTS


@respx.mock
async def test_client_error_is_not_retried(client: httpx.AsyncClient) -> None:
    route = respx.get(ALERTS_URL).respond(404)

    with pytest.raises(FetchError, match="HTTP 404") as exc:
        await fetch_alerts(client, SPOT, SETTINGS)

    assert exc.value.status_code == 404
    assert route.call_count == 1


@respx.mock
async def test_connection_error_is_not_retried(client: httpx.AsyncClient) -> None:
    route = respx.get(FORECAST_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(FetchError, match="request failed"):
        await fetch_forecast(client, SPOT, SETTINGS)

    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    ("body", "message"),
    [("not json", "not valid JSON"), ("[1, 2]", "not an object")],
)
async def test_bad_response_body(
    client: httpx.AsyncClient, body: str, message: str
) -> None:
    respx.get(FORECAST_URL).respond(200, text=body)

    with pytest.raises(FetchError, match=message):
        await fetch_forecast(client, SPOT, SETTINGS)


@respx.mock
async def test_failures_are_logged(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(ALERTS_URL).respond(404)

    with caplog.at_level(logging.WARNING), pytest.raises(FetchError):
        await fetch_alerts(client, SPOT, SETTINGS)

    assert "fetch_failed source=nws status=404 retry=no" in caplog.text


@respx.mock
@pytest.mark.parametrize("status", [301, 302, 304])
async def test_redirects_are_failures_and_not_retried(
    client: httpx.AsyncClient, status: int
) -> None:
    route = respx.get(FORECAST_URL).respond(status)

    with pytest.raises(FetchError, match=f"HTTP {status}") as exc:
        await fetch_forecast(client, SPOT, SETTINGS)

    assert exc.value.status_code == status
    assert route.call_count == 1


@respx.mock
async def test_request_that_cannot_be_built_is_a_fetch_error(
    client: httpx.AsyncClient,
) -> None:
    route = respx.get(ALERTS_URL).respond(json={})
    settings = Settings(
        nws_user_agent="Café app (me@example.com)", timezone="America/New_York"
    )

    with pytest.raises(FetchError, match="request failed: UnicodeEncodeError") as exc:
        await fetch_alerts(client, SPOT, settings)

    assert exc.value.source == "nws"
    assert route.call_count == 0


# --- raw-response callback (used by --save-raw) ---


@respx.mock
async def test_forecast_passes_raw_response_to_callback(
    client: httpx.AsyncClient,
) -> None:
    respx.get(FORECAST_URL).respond(json=load_fixture("open_meteo_sunny.json"))
    calls: list[tuple[str, str, dict[str, object]]] = []

    await fetch_forecast(
        client, SPOT, SETTINGS, on_raw=lambda *args: calls.append(args)
    )

    assert calls == [(SPOT.id, "open_meteo", load_fixture("open_meteo_sunny.json"))]


@respx.mock
async def test_alerts_passes_raw_response_to_callback(
    client: httpx.AsyncClient,
) -> None:
    respx.get(ALERTS_URL).respond(json=load_fixture("nws_alerts_winter_storm.json"))
    calls: list[tuple[str, str, dict[str, object]]] = []

    await fetch_alerts(client, SPOT, SETTINGS, on_raw=lambda *args: calls.append(args))

    assert calls == [(SPOT.id, "nws", load_fixture("nws_alerts_winter_storm.json"))]


@respx.mock
async def test_callback_is_not_called_for_failed_requests(
    client: httpx.AsyncClient,
) -> None:
    respx.get(FORECAST_URL).respond(404)
    calls: list[object] = []

    with pytest.raises(FetchError):
        await fetch_forecast(client, SPOT, SETTINGS, on_raw=calls.append)

    assert calls == []


@respx.mock
async def test_failing_callback_does_not_fail_the_request(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(ALERTS_URL).respond(json=load_fixture("nws_alerts_none.json"))

    def broken(spot_id: str, source: str, raw: dict[str, object]) -> None:
        raise OSError("disk full")

    with caplog.at_level(logging.WARNING):
        result = await fetch_alerts(client, SPOT, SETTINGS, on_raw=broken)

    assert result == load_fixture("nws_alerts_none.json")
    assert "raw_callback_failed spot=bristol source=nws error='OSError: disk full'" in (
        caplog.text
    )
