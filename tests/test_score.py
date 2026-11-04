import dataclasses
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from builders import (
    COLD_CALM_HOUR,
    ET,
    GOOD_HOUR,
    SUNRISE_HOUR,
    SUNSET_HOUR,
    THRESHOLDS,
    TODAY,
    TOMORROW,
    TZ,
    XC_THRESHOLDS,
    at,
    build,
    day_edits,
    make_spot,
)
from gooddays.fetch.service import normalize_alerts, normalize_forecast
from gooddays.models import (
    ActivityScore,
    Alert,
    DaylightWindow,
    HourlyConditions,
    SpotConditions,
    Thresholds,
)
from gooddays.score import score_conditions
from helpers import load_fixture


def score(
    conditions: SpotConditions,
    activity: str,
    day: date = TODAY,
    thresholds: Thresholds = THRESHOLDS,
) -> ActivityScore:
    (spot_score,) = score_conditions([conditions], thresholds, TZ).results
    (found,) = [s for s in spot_score.scores if s.activity == activity and s.day == day]
    return found


def failed(result: ActivityScore) -> set[str]:
    return {c.name for c in result.checks if not c.passed}


DAYLIGHT = range(SUNRISE_HOUR, SUNSET_HOUR)


# --- structure ---


def test_one_result_per_activity_per_day_for_today_and_tomorrow() -> None:
    conditions = build(activities=("hiking", "downhill_skiing", "biking"))

    (spot_score,) = score_conditions([conditions], THRESHOLDS, TZ).results

    assert [(s.activity, s.day) for s in spot_score.scores] == [
        ("hiking", TODAY),
        ("hiking", TOMORROW),
        ("downhill_skiing", TODAY),
        ("downhill_skiing", TOMORROW),
        ("biking", TODAY),
        ("biking", TOMORROW),
    ]


def test_today_is_the_fetch_date_in_the_settings_timezone() -> None:
    # 02:00 UTC on Jan 16 is still 21:00 on Jan 15 in New York.
    conditions = build(fetched_at=datetime(2026, 1, 16, 2, 0, tzinfo=UTC))

    (spot_score,) = score_conditions([conditions], THRESHOLDS, TZ).results

    assert {s.day for s in spot_score.scores} == {TODAY, TOMORROW}


def test_no_spots_means_no_scores() -> None:
    assert score_conditions([], THRESHOLDS, TZ).results == ()


def test_spots_are_scored_independently() -> None:
    cold = build(base={**GOOD_HOUR, "temperature_f": 10.0}, activities=("hiking",))
    warm = build(activities=("hiking",))

    first, second = score_conditions([cold, warm], THRESHOLDS, TZ).results

    assert first.scores[0].status == "not_good"
    assert second.scores[0].status == "good"


def test_biking_has_no_rules_yet() -> None:
    result = score(build(activities=("biking",)), "biking")

    assert result.status == "no_rules"
    assert result.checks == ()
    assert result.notes == ("no rules yet",)


def test_activities_are_scored_separately() -> None:
    # A rainy day fails hiking but doesn't touch skiing.
    edits = {
        **day_edits(
            TODAY, DAYLIGHT, precipitation_probability_pct=90, **COLD_CALM_HOUR
        ),
        (TODAY, 3): {"snowfall_in": 0.4},
    }
    conditions = build(edits=edits)

    assert score(conditions, "hiking").status == "not_good"
    assert score(conditions, "downhill_skiing").status == "good"


# --- hiking ---


def test_good_hiking_day() -> None:
    result = score(build(), "hiking")

    assert result.status == "good"
    assert result.reasons == ()
    assert all(c.passed for c in result.checks)


@pytest.mark.parametrize(
    ("field", "good", "bad", "check"),
    [
        ("temperature_f", 45.0, 44.9, "temperature_f"),
        ("precipitation_probability_pct", 30, 31, "precipitation_probability_pct"),
        ("precipitation_in", 0.1, 0.11, "precipitation_in"),
    ],
)
def test_hiking_boundaries(field: str, good: Any, bad: Any, check: str) -> None:
    passes = score(build(edits=day_edits(TODAY, DAYLIGHT, **{field: good})), "hiking")
    fails = score(build(edits=day_edits(TODAY, DAYLIGHT, **{field: bad})), "hiking")

    assert passes.status == "good"
    assert fails.status == "not_good"
    assert failed(fails) == {check, "usable_run"}
    assert any(r.startswith(f"{check}:") for r in fails.reasons)


def test_hiking_ignores_hours_outside_daylight() -> None:
    edits = {
        (TODAY, 2): {"temperature_f": -10.0},
        (TODAY, 22): {"precipitation_in": 2.0},
    }

    assert score(build(edits=edits), "hiking").status == "good"


def test_a_failing_check_shows_value_and_limit() -> None:
    result = score(
        build(edits=day_edits(TODAY, DAYLIGHT, temperature_f=41.0)), "hiking"
    )

    (check,) = [c for c in result.checks if c.name == "temperature_f"]
    assert check.limit == ">= 45 F"
    assert "0/10 hours pass" in check.value
    assert "first fail 07:00 -0500: 41" in check.value


@pytest.mark.parametrize(
    ("good_hours", "expected"),
    [
        (range(10, 14), "good"),  # 4 in a row
        (range(10, 13), "not_good"),  # only 3
        (list(range(8, 10)) + list(range(11, 13)), "not_good"),  # 2 + 2, gap between
    ],
)
def test_a_day_needs_a_run_of_consecutive_good_hours(
    good_hours: Any, expected: str
) -> None:
    edits = day_edits(TODAY, DAYLIGHT, temperature_f=30.0)  # all cold...
    edits.update({(TODAY, h): {"temperature_f": 60.0} for h in good_hours})

    result = score(build(edits=edits), "hiking")

    assert result.status == expected


def test_the_required_run_length_comes_from_the_thresholds() -> None:
    edits = day_edits(TODAY, DAYLIGHT, temperature_f=30.0)
    edits.update({(TODAY, h): {"temperature_f": 60.0} for h in range(10, 13)})
    easier = dataclasses.replace(
        THRESHOLDS, hiking=dataclasses.replace(THRESHOLDS.hiking, min_run_hours=3)
    )

    assert score(build(edits=edits), "hiking").status == "not_good"
    assert score(build(edits=edits), "hiking", thresholds=easier).status == "good"


# --- missing data ---


def test_a_series_that_is_entirely_missing_is_called_out() -> None:
    edits = day_edits(TODAY, DAYLIGHT, temperature_f=None)

    result = score(build(edits=edits), "hiking")

    assert result.status == "not_good"
    (check,) = [c for c in result.checks if c.name == "temperature_f"]
    assert not check.passed
    assert "no values for this reading in the data" in check.value
    assert "first fail" not in check.value


def test_a_value_missing_in_some_hours_names_the_first_one() -> None:
    edits = day_edits(TODAY, DAYLIGHT, temperature_f=30.0)
    edits[(TODAY, 8)] = {"temperature_f": None}

    result = score(build(edits=edits), "hiking")

    (check,) = [c for c in result.checks if c.name == "temperature_f"]
    assert "first fail 07:00 -0500: 30" in check.value  # the first failing hour


def test_a_missing_value_does_not_pass() -> None:
    edits = day_edits(TODAY, DAYLIGHT, temperature_f=60.0)
    edits.update(day_edits(TODAY, range(10, 13), temperature_f=None))  # 3 + gap + 4

    result = score(build(edits=edits), "hiking")

    (check,) = [c for c in result.checks if c.name == "temperature_f"]
    assert "first fail 10:00 -0500: missing" in check.value


def test_one_missing_hour_breaks_a_run() -> None:
    edits = day_edits(TODAY, DAYLIGHT, temperature_f=30.0)
    edits.update({(TODAY, h): {"temperature_f": 60.0} for h in range(8, 15)})
    edits[(TODAY, 11)] = {"temperature_f": None}  # 3 good, a hole, 3 good

    assert score(build(edits=edits), "hiking").status == "not_good"


def test_a_day_without_a_daylight_window_is_no_data() -> None:
    result = score(build(daylight=False), "hiking")

    assert result.status == "not_good"
    assert "no data: no daylight window" in result.reasons


def test_a_day_without_hourly_rows_is_no_data() -> None:
    conditions = dataclasses.replace(build(), hourly=())

    result = score(conditions, "hiking")

    assert result.status == "not_good"
    assert failed(result) == {"hourly_data"}
    assert result.checks[0].value == "no data"


def test_only_one_day_of_data_scores_the_other_as_no_data() -> None:
    result = score(build(days=(TODAY,)), "hiking", TOMORROW)

    assert result.status == "not_good"
    assert "no data: no daylight window" in result.reasons
    assert score(build(days=(TODAY,)), "hiking", TODAY).status == "good"


# --- partly over today ---


def test_only_the_remaining_hours_of_today_count() -> None:
    # 14:30: the 14:00, 15:00 and 16:00 rows remain: 3 hours, 4 are needed.
    result = score(build(fetched_at=at(TODAY, 14, 30)), "hiking")

    assert result.status == "not_good"
    assert failed(result) == {"hours_left"}
    assert "not enough hours left (3 h)" in result.reasons[0]


def test_the_hour_containing_the_fetch_time_counts() -> None:
    # 13:30: the 13:00 row is still in progress, so 13:00 to 16:00 is 4 hours.
    assert score(build(fetched_at=at(TODAY, 13, 30)), "hiking").status == "good"


def test_past_hours_with_bad_weather_dont_matter_today() -> None:
    edits = day_edits(TODAY, range(7, 11), temperature_f=10.0)  # a cold morning

    assert (
        score(build(edits=edits, fetched_at=at(TODAY, 11, 15)), "hiking").status
        == "good"
    )


def test_tomorrow_is_not_trimmed_by_the_fetch_time() -> None:
    assert (
        score(build(fetched_at=at(TODAY, 23, 30)), "hiking", TOMORROW).status == "good"
    )


def test_a_fully_past_daylight_window_has_no_hours_left() -> None:
    result = score(build(fetched_at=at(TODAY, 20, 0)), "hiking")

    assert failed(result) == {"hours_left"}
    assert "not enough hours left (0 h)" in result.reasons[0]


# --- alerts ---


def alert(
    severity: str | None, ends: datetime | None = None, event: str = "Storm"
) -> Alert:
    return Alert(event=event, severity=severity, headline=None, ends=ends)


def test_unknown_alerts_never_pass() -> None:
    result = score(build(alerts=None), "hiking")

    assert result.status == "not_good"
    assert failed(result) == {"alerts"}
    assert any(r.startswith("alerts: unknown") for r in result.reasons)


def test_no_active_alerts_do_not_fail() -> None:
    assert score(build(alerts=()), "hiking").status == "good"


@pytest.mark.parametrize("severity", ["Severe", "Extreme", "SEVERE", "extreme"])
def test_severe_alerts_fail_the_day(severity: str) -> None:
    result = score(build(alerts=(alert(severity),)), "hiking")

    assert result.status == "not_good"
    assert failed(result) == {"alerts"}
    assert f"Storm ({severity})" in result.reasons[0]


@pytest.mark.parametrize("severity", ["Moderate", "Minor", "Unknown", None])
def test_other_alerts_are_listed_but_do_not_fail(severity: str | None) -> None:
    result = score(build(alerts=(alert(severity),)), "hiking")

    assert result.status == "good"
    assert result.notes == (f"alert: Storm ({severity or 'severity n/a'})",)


def test_severe_alerts_fail_every_activity() -> None:
    conditions = build(
        alerts=(alert("Severe"),),
        edits={
            (TODAY, 3): {"snowfall_in": 1.0},
            **day_edits(TODAY, DAYLIGHT, **COLD_CALM_HOUR),
        },
    )

    assert score(conditions, "hiking").status == "not_good"
    assert score(conditions, "downhill_skiing").status == "not_good"


def test_an_alert_with_no_end_time_is_active() -> None:
    assert score(build(alerts=(alert("Severe", None),)), "hiking").status == "not_good"


def test_an_alert_that_ended_before_the_window_is_ignored() -> None:
    ended = at(TODAY, SUNRISE_HOUR) - timedelta(minutes=1)

    assert score(build(alerts=(alert("Severe", ended),)), "hiking").status == "good"


def test_an_alert_ending_exactly_at_the_window_start_is_not_active() -> None:
    boundary = at(TODAY, SUNRISE_HOUR)

    assert score(build(alerts=(alert("Severe", boundary),)), "hiking").status == "good"


def test_an_alert_ending_just_after_the_window_start_is_active() -> None:
    just_after = at(TODAY, SUNRISE_HOUR) + timedelta(minutes=1)

    assert (
        score(build(alerts=(alert("Severe", just_after),)), "hiking").status
        == "not_good"
    )


def test_a_real_winter_storm_alert_fails_the_day() -> None:
    storm = normalize_alerts(
        load_fixture("nws_alerts_winter_storm.json")
    )  # ends 2026-01-16 07:00
    conditions = build(alerts=storm)

    assert score(conditions, "hiking", TODAY).status == "not_good"
    # Tomorrow's window starts at 07:00, exactly when the alert ends: not active.
    assert score(conditions, "hiking", TOMORROW).status == "good"


def test_skiing_alert_window_starts_overnight() -> None:
    # Ends 03:00, after today's overnight window starts (00:00) but before sunrise.
    edits = {
        (TODAY, 3): {"snowfall_in": 1.0},
        **day_edits(TODAY, DAYLIGHT, **COLD_CALM_HOUR),
    }
    ends_overnight = at(TODAY, 3)

    skiing = score(
        build(edits=edits, alerts=(alert("Severe", ends_overnight),)), "downhill_skiing"
    )
    hiking = score(
        build(edits=edits, alerts=(alert("Severe", ends_overnight),)), "hiking"
    )

    assert "alerts" in failed(skiing)  # active during the night being scored
    assert "alerts" not in failed(hiking)  # hiking's window starts at sunrise, later


# --- skiing ---


def ski_day(
    day: date = TODAY, **overrides: Any
) -> dict[tuple[date, int], dict[str, Any]]:
    """Cold, calm daylight hours plus snow at 03:00 that morning."""
    edits = day_edits(day, DAYLIGHT, **COLD_CALM_HOUR)
    edits[(day, 3)] = {"snowfall_in": 0.4}
    edits.update(overrides)
    return edits


def test_good_ski_day() -> None:
    result = score(build(edits=ski_day()), "downhill_skiing")

    assert result.status == "good"
    assert {c.name for c in result.checks} == {
        "temperature_f",
        "wind_speed_mph",
        "usable_run",
        "snow",
        "alerts",
    }


def test_snow_on_the_ground_passes_without_new_snow() -> None:
    edits = day_edits(TODAY, DAYLIGHT, **COLD_CALM_HOUR, snow_depth_in=6.0)

    result = score(build(edits=edits), "downhill_skiing")

    assert result.status == "good"
    (snow,) = [c for c in result.checks if c.name == "snow"]
    assert "snow depth up to 6.0 in" in snow.value


def test_snow_is_required() -> None:
    edits = day_edits(TODAY, DAYLIGHT, **COLD_CALM_HOUR)  # cold and calm, no snow

    result = score(build(edits=edits), "downhill_skiing")

    assert result.status == "not_good"
    assert failed(result) == {"snow"}
    assert "no snow overnight and none on the ground" in result.reasons[0]


def test_snow_with_no_data_is_no_data() -> None:
    edits = day_edits(TODAY, range(0, 24), snowfall_in=None, snow_depth_in=None)
    edits.update(
        day_edits(
            TODAY, DAYLIGHT, snowfall_in=None, snow_depth_in=None, **COLD_CALM_HOUR
        )
    )

    result = score(build(edits=edits), "downhill_skiing")

    assert result.status == "not_good"
    (snow,) = [c for c in result.checks if c.name == "snow"]
    assert snow.value == "no data"


def test_snow_checks_are_strictly_above_the_minimums() -> None:
    # Exactly the minimum (0) is not snow, for either snowfall or depth.
    edits = day_edits(TODAY, DAYLIGHT, **COLD_CALM_HOUR, snow_depth_in=0.0)
    edits[(TODAY, 3)] = {"snowfall_in": 0.0}
    assert score(build(edits=edits), "downhill_skiing").status == "not_good"

    stricter = dataclasses.replace(
        THRESHOLDS,
        downhill_skiing=dataclasses.replace(
            THRESHOLDS.downhill_skiing, min_snowfall_in=0.5
        ),
    )
    edits[(TODAY, 3)] = {"snowfall_in": 0.5}
    assert (
        score(build(edits=edits), "downhill_skiing", thresholds=stricter).status
        == "not_good"
    )
    edits[(TODAY, 3)] = {"snowfall_in": 0.51}
    assert (
        score(build(edits=edits), "downhill_skiing", thresholds=stricter).status
        == "good"
    )


def test_a_snow_weather_code_alone_does_not_count() -> None:
    edits = ski_day()
    edits[(TODAY, 3)] = {
        "snowfall_in": 0.0,
        "weather_code": 73,
    }  # snow code, no snowfall

    assert score(build(edits=edits), "downhill_skiing").status == "not_good"


@pytest.mark.parametrize(
    ("field", "good", "bad"),
    [("temperature_f", 31.9, 32.0), ("wind_speed_mph", 9.9, 10.0)],
)
def test_ski_boundaries_are_strict(field: str, good: float, bad: float) -> None:
    base = {**COLD_CALM_HOUR}
    passes = ski_day()
    passes.update(day_edits(TODAY, DAYLIGHT, **{**base, field: good}))
    fails = dict(passes)
    fails.update(day_edits(TODAY, DAYLIGHT, **{**base, field: bad}))

    assert score(build(edits=passes), "downhill_skiing").status == "good"
    result = score(build(edits=fails), "downhill_skiing")
    assert result.status == "not_good"
    assert field in failed(result)


def test_overnight_window_for_today_runs_from_the_start_of_the_data() -> None:
    edits = day_edits(TODAY, DAYLIGHT, **COLD_CALM_HOUR)
    edits[(TODAY, 0)] = {"snowfall_in": 0.3}  # the very first hour of the data

    assert score(build(edits=edits), "downhill_skiing").status == "good"


def test_overnight_window_for_tomorrow_starts_at_todays_sunset() -> None:
    cold_tomorrow = day_edits(TOMORROW, DAYLIGHT, **COLD_CALM_HOUR)
    after_sunset = {**cold_tomorrow, (TODAY, SUNSET_HOUR): {"snowfall_in": 0.4}}
    during_today = {**cold_tomorrow, (TODAY, 12): {"snowfall_in": 0.4}}

    assert (
        score(build(edits=after_sunset), "downhill_skiing", TOMORROW).status == "good"
    )
    # Snow that fell in the middle of today isn't "overnight" for tomorrow.
    assert (
        score(build(edits=during_today), "downhill_skiing", TOMORROW).status
        == "not_good"
    )


def test_overnight_snow_after_sunrise_does_not_count_as_overnight() -> None:
    edits = day_edits(TODAY, DAYLIGHT, **COLD_CALM_HOUR)
    edits[(TODAY, SUNRISE_HOUR)] = {**COLD_CALM_HOUR, "snowfall_in": 0.4}

    assert score(build(edits=edits), "downhill_skiing").status == "not_good"


def test_overnight_snow_is_not_trimmed_by_the_fetch_time() -> None:
    conditions = build(edits=ski_day(), fetched_at=at(TODAY, 6, 30))

    assert score(conditions, "downhill_skiing").status == "good"


def test_ski_day_without_a_daylight_window_is_no_data() -> None:
    result = score(build(edits=ski_day(), daylight=False), "downhill_skiing")

    assert result.status == "not_good"
    assert "no data: no daylight window" in result.reasons


def test_ski_temperature_and_wind_are_checked_in_daylight_only() -> None:
    edits = ski_day()
    edits[(TODAY, 20)] = {"temperature_f": 70.0, "wind_speed_mph": 40.0}  # evening

    assert score(build(edits=edits), "downhill_skiing").status == "good"


# --- daylight saving time ---


def real_fall_back_day() -> SpotConditions:
    hourly, daylight = normalize_forecast(
        load_fixture("open_meteo_dst_fall_back.json"), TZ
    )
    return SpotConditions(
        spot=make_spot("hiking", "downhill_skiing"),
        fetched_at=datetime(2026, 11, 1, 4, 30, tzinfo=UTC),  # 00:30 EDT on Nov 1
        hourly=hourly,
        daylight=daylight,
        alerts=(),
    )


def test_fall_back_day_overnight_window_includes_both_1am_hours() -> None:
    conditions = real_fall_back_day()
    first_1am, second_1am = conditions.hourly[1], conditions.hourly[2]
    assert (first_1am.time.hour, second_1am.time.hour) == (1, 1)

    # Snow only in the repeated hour, which is before sunrise (06:43 EST).
    snowy = dataclasses.replace(second_1am, snowfall_in=0.6)
    hourly = (*conditions.hourly[:2], snowy, *conditions.hourly[3:])
    result = score(
        dataclasses.replace(conditions, hourly=hourly),
        "downhill_skiing",
        date(2026, 11, 1),
    )

    (snow,) = [c for c in result.checks if c.name == "snow"]
    assert snow.passed
    assert "overnight snowfall 0.60 in" in snow.value


def test_run_of_hours_is_continuous_across_the_repeated_hour() -> None:
    # A window from 00:30 EDT to 05:30 EST holds six rows: 01:00 EDT, 01:00 EST,
    # then 02:00 to 05:00. Compared by wall-clock time that run would look broken.
    day = date(2026, 11, 1)
    utc_hours = [
        datetime(2026, 11, 1, h, tzinfo=UTC).astimezone(ET) for h in range(4, 12)
    ]
    hourly = tuple(HourlyConditions(time=t, **GOOD_HOUR) for t in utc_hours)
    window = DaylightWindow(
        day,
        datetime(2026, 11, 1, 4, 30, tzinfo=UTC).astimezone(ET),
        datetime(2026, 11, 1, 10, 30, tzinfo=UTC).astimezone(ET),
    )
    conditions = SpotConditions(
        spot=make_spot("hiking"),
        fetched_at=datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
        hourly=hourly,
        daylight=(window,),
        alerts=(),
    )
    six = dataclasses.replace(
        THRESHOLDS, hiking=dataclasses.replace(THRESHOLDS.hiking, min_run_hours=6)
    )

    result = score(conditions, "hiking", day, thresholds=six)

    assert result.status == "good"
    (run,) = [c for c in result.checks if c.name == "usable_run"]
    assert run.value == "longest passing run 6 h of 6 usable h"


def test_the_real_forecast_fixture_scores_end_to_end() -> None:
    hourly, daylight = normalize_forecast(load_fixture("open_meteo_warm_dry.json"), TZ)
    conditions = SpotConditions(
        spot=make_spot("hiking", "downhill_skiing"),
        fetched_at=datetime(2026, 6, 15, 4, 0, tzinfo=ET),
        hourly=hourly,
        daylight=daylight,
        alerts=(),
    )

    (spot_score,) = score_conditions([conditions], THRESHOLDS, TZ).results

    by_key = {(s.activity, s.day): s.status for s in spot_score.scores}
    assert by_key[("hiking", date(2026, 6, 15))] == "good"
    assert by_key[("hiking", date(2026, 6, 16))] == "good"
    assert by_key[("downhill_skiing", date(2026, 6, 15))] == "not_good"  # warm, no snow


# --- cross-country skiing ---

XC = "cross_country_skiing"
XC_GOOD: dict[str, Any] = {
    "temperature_f": 20.0,
    "wind_speed_mph": 3.0,
    "snow_depth_in": 6.0,
    "precipitation_probability_pct": 10,
    "rain_in": 0.0,
}


def xc_build(
    edits: dict[tuple[date, int], dict[str, Any]] | None = None,
    **kwargs: Any,
) -> SpotConditions:
    """A cold, calm, snowy day (good for cross-country) with `edits` on top."""
    base = {**GOOD_HOUR, **XC_GOOD}
    return build(base=base, edits=edits, activities=(XC,), **kwargs)


def xc_thresholds(**changes: Any) -> Thresholds:
    return dataclasses.replace(
        THRESHOLDS,
        cross_country_skiing=dataclasses.replace(XC_THRESHOLDS, **changes),
    )


def test_good_cross_country_day() -> None:
    result = score(xc_build(), XC)

    assert result.status == "good"
    assert result.reasons == ()
    assert {c.name for c in result.checks} == {
        "snow_depth_in",
        "temperature_f",
        "wind_speed_mph",
        "precipitation_probability_pct",
        "rain_in",
        "usable_run",
        "alerts",
    }


def test_cross_country_gets_a_result_for_today_and_tomorrow() -> None:
    (spot_score,) = score_conditions([xc_build()], THRESHOLDS, TZ).results

    assert [(s.activity, s.day, s.status) for s in spot_score.scores] == [
        (XC, TODAY, "good"),
        (XC, TOMORROW, "good"),
    ]


@pytest.mark.parametrize(
    ("depth", "expected"),
    [(5.0, "good"), (4.9, "not_good"), (3.0, "not_good"), (0.0, "not_good")],
)
def test_cross_country_needs_five_inches_of_snow(depth: float, expected: str) -> None:
    edits = day_edits(TODAY, DAYLIGHT, snow_depth_in=depth)

    result = score(xc_build(edits), XC)

    assert result.status == expected
    if expected == "not_good":
        assert "snow_depth_in" in failed(result)


@pytest.mark.parametrize(
    ("temperature", "expected"),
    [
        (31.9, "good"),
        (32.0, "not_good"),  # strictly below 32
        (50.0, "not_good"),  # a warm day with snow on the ground
        (10.0, "good"),  # at least 10
        (9.9, "not_good"),  # too cold
        (-5.0, "not_good"),
    ],
)
def test_cross_country_temperature_window(temperature: float, expected: str) -> None:
    edits = day_edits(TODAY, DAYLIGHT, temperature_f=temperature)

    result = score(xc_build(edits), XC)

    assert result.status == expected
    if expected == "not_good":
        (check,) = [c for c in result.checks if c.name == "temperature_f"]
        assert check.limit == ">= 10 and < 32 F"
        assert not check.passed


@pytest.mark.parametrize(
    "wind, expected", [(9.9, "good"), (10.0, "not_good"), (25.0, "not_good")]
)
def test_cross_country_wind_is_strictly_below_the_limit(
    wind: float, expected: str
) -> None:
    edits = day_edits(TODAY, DAYLIGHT, wind_speed_mph=wind)

    assert score(xc_build(edits), XC).status == expected


@pytest.mark.parametrize("chance, expected", [(30, "good"), (31, "not_good")])
def test_cross_country_rain_chance_limit_is_inclusive(
    chance: int, expected: str
) -> None:
    edits = day_edits(TODAY, DAYLIGHT, precipitation_probability_pct=chance)

    assert score(xc_build(edits), XC).status == expected


def test_any_rain_fails_cross_country() -> None:
    edits = day_edits(TODAY, DAYLIGHT, rain_in=0.001)

    result = score(xc_build(edits), XC)

    assert result.status == "not_good"
    assert "rain_in" in failed(result)
    (check,) = [c for c in result.checks if c.name == "rain_in"]
    assert check.limit == "<= 0 in per hour"


def test_snowfall_is_not_rain() -> None:
    # Total precipitation includes snow's water; only liquid rain counts as rain.
    edits = day_edits(
        TODAY, DAYLIGHT, precipitation_in=0.2, snowfall_in=1.5, rain_in=0.0
    )

    assert score(xc_build(edits), XC).status == "good"


def test_fresh_snow_overnight_is_not_required() -> None:
    # Depth is fine but nothing fell overnight or at all: still good.
    edits = day_edits(TODAY, range(0, 24), snowfall_in=0.0)

    assert score(xc_build(edits), XC).status == "good"


@pytest.mark.parametrize(
    "field",
    [
        "snow_depth_in",
        "temperature_f",
        "wind_speed_mph",
        "precipitation_probability_pct",
        "rain_in",
    ],
)
def test_cross_country_missing_values_do_not_pass(field: str) -> None:
    edits = day_edits(TODAY, DAYLIGHT, **{field: None})

    result = score(xc_build(edits), XC)

    assert result.status == "not_good"
    (check,) = [c for c in result.checks if c.name == field]
    assert not check.passed
    assert "no values for this reading in the data" in check.value


def test_a_midday_thaw_breaks_the_run() -> None:
    # 07-09 is cold (3 hours), 10-13 thaws, 14-16 is cold again (3 hours).
    edits = day_edits(TODAY, range(10, 14), temperature_f=40.0)

    result = score(xc_build(edits), XC)

    assert result.status == "not_good"
    assert failed(result) == {"temperature_f", "usable_run"}  # the thaw hours fail


def test_three_good_hours_then_a_gap_then_three_is_not_a_run() -> None:
    edits = day_edits(TODAY, DAYLIGHT, snow_depth_in=0.0)
    edits.update({(TODAY, h): {"snow_depth_in": 6.0} for h in (7, 8, 9, 11, 12, 13)})

    assert score(xc_build(edits), XC).status == "not_good"


def test_cross_country_thresholds_come_from_the_config() -> None:
    conditions = xc_build()

    assert score(conditions, XC).status == "good"  # 6 in of snow, limit 5
    stricter = xc_thresholds(min_snow_depth_in=8)
    assert score(conditions, XC, thresholds=stricter).status == "not_good"
    windier = xc_thresholds(max_wind_mph=2)
    assert score(conditions, XC, thresholds=windier).status == "not_good"
    warmer = xc_thresholds(max_temperature_f=15)
    assert score(conditions, XC, thresholds=warmer).status == "not_good"


def test_cross_country_without_a_daylight_window_is_no_data() -> None:
    result = score(xc_build(daylight=False), XC)

    assert result.status == "not_good"
    assert "no data: no daylight window" in result.reasons


def test_cross_country_without_hourly_rows_is_no_data() -> None:
    result = score(dataclasses.replace(xc_build(), hourly=()), XC)

    assert failed(result) == {"hourly_data"}


def test_cross_country_only_counts_the_remaining_hours_today() -> None:
    result = score(xc_build(fetched_at=at(TODAY, 14, 30)), XC)

    assert failed(result) == {"hours_left"}
    assert score(xc_build(fetched_at=at(TODAY, 13, 30)), XC).status == "good"


def test_cross_country_alerts_follow_the_same_rules() -> None:
    assert score(xc_build(alerts=None), XC).status == "not_good"
    assert score(xc_build(alerts=(alert("Severe"),)), XC).status == "not_good"
    assert score(xc_build(alerts=(alert("Extreme"),)), XC).status == "not_good"
    minor = score(xc_build(alerts=(alert("Minor"),)), XC)
    assert minor.status == "good"
    assert minor.notes == ("alert: Storm (Minor)",)


def test_cross_country_alert_window_starts_at_sunrise() -> None:
    # Ends overnight, before sunrise: over by the time the window starts.
    overnight = alert("Severe", at(TODAY, 3))

    assert score(xc_build(alerts=(overnight,)), XC).status == "good"
    # Downhill's window starts overnight, so the same alert fails it.
    downhill = build(
        base={**GOOD_HOUR, **COLD_CALM_HOUR},
        edits={(TODAY, 3): {"snowfall_in": 1.0}},
        activities=("downhill_skiing",),
        alerts=(overnight,),
    )
    assert "alerts" in failed(score(downhill, "downhill_skiing"))


def test_hiking_and_cross_country_are_scored_independently() -> None:
    cold = build(
        base={**GOOD_HOUR, **XC_GOOD}, activities=("hiking", XC)
    )  # cold and snowy: bad for hiking, good for cross-country
    warm = build(
        base={**GOOD_HOUR, "temperature_f": 60.0, "snow_depth_in": 6.0},
        activities=("hiking", XC),
    )  # warm: good for hiking, too warm for cross-country

    assert score(cold, "hiking").status == "not_good"
    assert score(cold, XC).status == "good"
    assert score(warm, "hiking").status == "good"
    assert score(warm, XC).status == "not_good"


def test_cross_country_and_downhill_can_disagree_on_the_same_snow() -> None:
    # 3 inches on the ground: any snow satisfies downhill, but not cross-country.
    base = {**GOOD_HOUR, **XC_GOOD, "snow_depth_in": 3.0}
    conditions = build(base=base, activities=("downhill_skiing", XC))

    assert score(conditions, "downhill_skiing").status == "good"
    assert score(conditions, XC).status == "not_good"


# --- review follow-ups ---


def downhill_with_snow(hours: dict[int, float], **thresholds: Any) -> ActivityScore:
    """A cold, calm downhill day with `hours` of overnight snowfall (inches)."""
    edits = day_edits(TODAY, DAYLIGHT, **COLD_CALM_HOUR)
    edits.update({(TODAY, h): {"snowfall_in": v} for h, v in hours.items()})
    stricter = dataclasses.replace(
        THRESHOLDS,
        downhill_skiing=dataclasses.replace(THRESHOLDS.downhill_skiing, **thresholds),
    )
    return score(build(edits=edits), "downhill_skiing", thresholds=stricter)


def test_downhill_snow_is_the_total_overnight_snowfall() -> None:
    six_light_hours = {h: 0.3 for h in range(1, 7)}  # 1.8 in in total

    # No single hour reaches 0.5, but the night's total does.
    result = downhill_with_snow(six_light_hours, min_snowfall_in=0.5)

    assert result.status == "good"
    (snow,) = [c for c in result.checks if c.name == "snow"]
    assert snow.value == "overnight snowfall 1.80 in"
    assert snow.limit == "overnight snowfall total > 0.5 in, or snow depth > 0 in"


def test_downhill_total_snowfall_must_be_strictly_above_the_minimum() -> None:
    assert (
        downhill_with_snow({1: 0.25, 2: 0.25}, min_snowfall_in=0.5).status == "not_good"
    )
    assert downhill_with_snow({1: 0.25, 2: 0.26}, min_snowfall_in=0.5).status == "good"


def test_downhill_snow_outside_the_overnight_window_is_not_added_to_the_total() -> None:
    # 0.3 in overnight and 0.3 in at noon (daytime): only 0.3 counts.
    result = downhill_with_snow({1: 0.3, 12: 0.3}, min_snowfall_in=0.5)

    assert result.status == "not_good"


def test_an_unhandled_activity_fails_loudly_instead_of_silently() -> None:
    conditions = dataclasses.replace(build(), spot=make_spot("surfing"))

    with pytest.raises(AssertionError):
        score_conditions([conditions], THRESHOLDS, TZ)


def test_downhill_without_the_previous_sunset_is_no_data_for_tomorrow() -> None:
    edits = {**ski_day(TOMORROW), (TODAY, 3): {"snowfall_in": 0.4}}
    conditions = build(edits=edits)
    tomorrow_only = tuple(w for w in conditions.daylight if w.date == TOMORROW)

    result = score(
        dataclasses.replace(conditions, daylight=tomorrow_only),
        "downhill_skiing",
        TOMORROW,
    )

    assert result.status == "not_good"
    assert "no data: no previous sunset to start the overnight window" in result.reasons
    assert "snow" not in {c.name for c in result.checks}  # not judged at all


def test_today_may_start_its_overnight_window_at_the_first_row() -> None:
    # Same data: today has no previous day in the data and that is fine.
    conditions = build(edits=ski_day())
    today_only = tuple(w for w in conditions.daylight if w.date == TODAY)

    result = score(
        dataclasses.replace(conditions, daylight=today_only), "downhill_skiing", TODAY
    )

    assert result.status == "good"


def test_a_severe_alert_alongside_a_minor_one() -> None:
    alerts = (alert("Minor", event="Wind Advisory"), alert("Severe", event="Blizzard"))

    result = score(build(alerts=alerts), "hiking")

    assert result.status == "not_good"
    (check,) = [c for c in result.checks if c.name == "alerts"]
    assert check.value == "Blizzard (Severe)"  # only the severe one fails the day
    assert result.notes == ("alert: Wind Advisory (Minor)",)


def test_a_daylight_window_shorter_than_the_required_run() -> None:
    conditions = build()
    short = DaylightWindow(TODAY, at(TODAY, 7), at(TODAY, 10))  # 07:00, 08:00, 09:00
    conditions = dataclasses.replace(
        conditions, daylight=(short, *conditions.daylight[1:])
    )

    result = score(conditions, "hiking")

    # Nothing was trimmed (we fetched at the start of the day), so this is not
    # "hours left": every check just never gets a run of 4 hours.
    assert "hours_left" not in failed(result)
    assert "usable_run" in failed(result)
    (run,) = [c for c in result.checks if c.name == "usable_run"]
    assert run.value == "longest passing run 3 h of 3 usable h"


def test_a_spring_forward_day_keeps_the_run_across_the_missing_hour() -> None:
    # 2026-03-08: 01:00 EST (06:00Z), then 03:00 EDT (07:00Z). A window from
    # 00:30 EST to 05:30 EDT holds four rows. By wall clock 01:00 -> 03:00 would
    # look like a 2-hour gap and break the run.
    day = date(2026, 3, 8)
    rows = [datetime(2026, 3, 8, h, tzinfo=UTC).astimezone(ET) for h in (6, 7, 8, 9)]
    hourly = tuple(HourlyConditions(time=t, **GOOD_HOUR) for t in rows)
    window = DaylightWindow(
        day,
        datetime(2026, 3, 8, 5, 30, tzinfo=UTC).astimezone(ET),
        datetime(2026, 3, 8, 9, 30, tzinfo=UTC).astimezone(ET),
    )
    conditions = SpotConditions(
        spot=make_spot("hiking"),
        fetched_at=datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
        hourly=hourly,
        daylight=(window,),
        alerts=(),
    )

    result = score(conditions, "hiking", day)

    assert [h.time.hour for h in hourly] == [1, 3, 4, 5]
    assert result.status == "good"
    (run,) = [c for c in result.checks if c.name == "usable_run"]
    assert run.value == "longest passing run 4 h of 4 usable h"
