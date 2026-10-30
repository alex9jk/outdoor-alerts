import json
from datetime import UTC, datetime
from typing import Any

from gooddays.fetch.service import normalize_alerts, normalize_forecast
from gooddays.models import Alert, FetchResult, Spot, SpotConditions, SpotError
from gooddays.report import to_json, to_text
from helpers import load_fixture

SPOT = Spot(
    id="bristol",
    name="Bristol",
    lat=42.745,
    lon=-77.404,
    activities=("skiing", "hiking"),
)


def conditions(
    forecast: str = "open_meteo_sunny.json",
    alerts: tuple[Alert, ...] | None = (),
) -> SpotConditions:
    hourly, daylight = normalize_forecast(load_fixture(forecast), "America/New_York")
    return SpotConditions(
        spot=SPOT,
        fetched_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        hourly=hourly,
        daylight=daylight,
        alerts=alerts,
    )


def result_of(
    *results: SpotConditions, errors: list[SpotError] | None = None
) -> FetchResult:
    return FetchResult(results=list(results), errors=errors or [])


# --- text ---


def test_text_summarizes_each_day() -> None:
    text = to_text(result_of(conditions()))

    assert "Bristol (bristol)  fetched " in text
    assert (
        "  2026-06-15  temp 68-75F  precip 0.00in  (max 5%)  wind max 6mph  "
        "gusts 11mph  snow 0.0in  daylight 05:25-20:35"
    ) in text
    assert text.endswith("1 spot(s) ok, 0 error(s)")


def test_text_shows_na_for_missing_values() -> None:
    text = to_text(result_of(conditions("open_meteo_missing_fields.json")))

    assert (
        "  2026-06-15  temp 60-62F  precip 0.00in  (max n/a)  wind max 7mph  "
        "gusts n/a  snow n/a  daylight 05:25-20:35"
    ) in text


def test_text_alert_states_are_distinct() -> None:
    none_active = to_text(result_of(conditions(alerts=())))
    unknown = to_text(result_of(conditions(alerts=None)))
    storm = to_text(
        result_of(
            conditions(
                alerts=normalize_alerts(load_fixture("nws_alerts_winter_storm.json"))
            )
        )
    )

    assert "Alerts: none" in none_active
    assert "Alerts: unknown (NWS check failed)" in unknown
    assert "Alerts: 1" in storm
    assert "- Winter Storm Warning (Severe), ends 2026-01-16 07:00 -0500" in storm


def test_text_lists_errors_and_counts() -> None:
    errors = [
        SpotError("spot-1", "open_meteo", "HTTP 500", 500),
        SpotError("spot-2", "normalize", "forecast: missing 'hourly' object"),
    ]

    text = to_text(result_of(conditions(), errors=errors))

    assert "ERROR spot-1 [open_meteo] HTTP 500 (status 500)" in text
    assert "ERROR spot-2 [normalize] forecast: missing 'hourly' object\n" in text + "\n"
    assert "(status None)" not in text
    assert text.endswith("1 spot(s) ok, 2 error(s)")


def test_text_with_no_results() -> None:
    assert to_text(result_of()) == "0 spot(s) ok, 0 error(s)"


# --- JSON ---


def parse(result: FetchResult) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(to_json(result))
    return data


def test_json_structure_and_iso_datetimes() -> None:
    data = parse(result_of(conditions()))

    (spot_json,) = data["results"]
    assert spot_json["spot"] == {
        "id": "bristol",
        "name": "Bristol",
        "lat": 42.745,
        "lon": -77.404,
        "activities": ["skiing", "hiking"],
    }
    assert spot_json["fetched_at"] == "2026-06-15T12:00:00+00:00"
    assert spot_json["hourly"][0]["time"] == "2026-06-15T09:00:00-04:00"
    assert spot_json["hourly"][0]["temperature_f"] == 68.0
    assert spot_json["daylight"][0]["date"] == "2026-06-15"
    assert spot_json["daylight"][0]["sunrise"] == "2026-06-15T05:25:00-04:00"
    assert data["errors"] == []


def test_json_distinguishes_unknown_none_and_present_alerts() -> None:
    storm = normalize_alerts(load_fixture("nws_alerts_winter_storm.json"))

    assert parse(result_of(conditions(alerts=None)))["results"][0]["alerts"] is None
    assert parse(result_of(conditions(alerts=())))["results"][0]["alerts"] == []
    (alert,) = parse(result_of(conditions(alerts=storm)))["results"][0]["alerts"]
    assert alert["event"] == "Winter Storm Warning"
    assert alert["ends"] == "2026-01-16T07:00:00-05:00"


def test_json_keeps_missing_values_as_null_and_includes_errors() -> None:
    errors = [SpotError("spot-1", "nws", "HTTP 503", 503)]

    data = parse(result_of(conditions("open_meteo_missing_fields.json"), errors=errors))

    hours = data["results"][0]["hourly"]
    assert hours[1]["temperature_f"] is None
    assert hours[0]["wind_gust_mph"] is None
    assert data["errors"] == [
        {
            "spot_id": "spot-1",
            "source": "nws",
            "message": "HTTP 503",
            "status_code": 503,
        }
    ]
