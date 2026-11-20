"""Strict JSON for the data the pipeline stages hand on, and back."""

import dataclasses
import json
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from builders import (
    ET,
    GOOD_HOUR,
    THRESHOLDS,
    TODAY,
    TZ,
    at,
    build,
    day_edits,
    make_spot,
)
from gooddays.message import build_facts, message_from_facts
from gooddays.models import (
    Alert,
    DaylightWindow,
    EntryKey,
    FetchResult,
    HourlyConditions,
    Message,
    SpotConditions,
    SpotError,
)
from gooddays.score import score_conditions
from gooddays.serialize import (
    MessageStage,
    SerializationError,
    UnsupportedSchemaVersionError,
    deserialize,
    deserialize_fetch,
    deserialize_message_stage,
    deserialize_score,
    serialize,
    serialize_fetch,
    serialize_message_stage,
    serialize_score,
)


def through_json(document: Any) -> Any:
    """What a store does to a document: text out, text back."""
    return json.loads(json.dumps(document, allow_nan=False))


def instants(value: Any) -> list[tuple[str, int]]:
    """Every datetime inside `value` as (UTC instant, offset in seconds), in order."""
    found: list[tuple[str, int]] = []

    def walk(v: Any) -> None:
        if isinstance(v, datetime):
            offset = v.utcoffset()
            assert offset is not None
            found.append((v.astimezone(UTC).isoformat(), int(offset.total_seconds())))
        elif dataclasses.is_dataclass(v) and not isinstance(v, type):
            for f in dataclasses.fields(v):
                walk(getattr(v, f.name))
        elif isinstance(v, tuple | list):
            for item in v:
                walk(item)

    walk(value)
    return found


def a_fetch_result() -> FetchResult:
    good = build(edits=day_edits(TODAY, range(7, 17), temperature_f=64.5))
    unknown_alerts = dataclasses.replace(
        build(activities=("hiking",)),
        spot=dataclasses.replace(make_spot("hiking"), id="spot-1", name="Spot 1"),
        alerts=None,
    )
    alert = Alert(
        event="Winter Storm Warning",
        severity="Severe",
        headline="Heavy snow",
        ends=datetime(2026, 1, 16, 6, 0, tzinfo=timezone_minus5()),
    )
    with_alert = dataclasses.replace(
        build(),
        spot=dataclasses.replace(make_spot("hiking"), id="spot-2", name="Spot 2"),
        alerts=(alert,),
    )
    return FetchResult(
        results=[good, unknown_alerts, with_alert],
        errors=[SpotError("spot-3", "open_meteo", "HTTP 500", 500)],
    )


def timezone_minus5() -> Any:
    from datetime import timezone

    return timezone(timedelta(hours=-5))


# --- round trips (AC5, AC6) ---


def test_a_fetch_result_round_trips_exactly() -> None:
    original = a_fetch_result()

    restored = deserialize_fetch(through_json(serialize_fetch(original)))

    assert restored == original
    assert isinstance(restored.results, list) and isinstance(restored.errors, list)
    assert isinstance(restored.results[0].hourly, tuple)


def test_unknown_alerts_and_no_alerts_stay_different() -> None:
    restored = deserialize_fetch(through_json(serialize_fetch(a_fetch_result())))

    assert restored.results[0].alerts == ()
    assert restored.results[1].alerts is None


def test_missing_readings_stay_none() -> None:
    hour = HourlyConditions(
        time=at(TODAY, 3), **{**GOOD_HOUR, "temperature_f": None, "weather_code": None}
    )
    conditions = dataclasses.replace(build(), hourly=(hour,))
    original = FetchResult(results=[conditions], errors=[])

    restored = deserialize_fetch(through_json(serialize_fetch(original)))

    assert restored == original
    assert restored.results[0].hourly[0].temperature_f is None


def test_a_score_result_round_trips_including_a_no_data_day() -> None:
    results = [build(), build(daylight=False)]
    original = score_conditions(results, THRESHOLDS, TZ)
    assert any(
        "no data" in " ".join(s.reasons) for r in original.results for s in r.scores
    )

    restored = deserialize_score(through_json(serialize_score(original)))

    assert restored == original


def test_the_message_stage_round_trips_with_covered_keys() -> None:
    conditions = [build(edits=day_edits(TODAY, range(7, 17), temperature_f=64.5))]
    scores = score_conditions(conditions, THRESHOLDS, TZ)
    spots = [c.spot for c in conditions]
    errors = [SpotError("x", "nws", "HTTP 503", 503)]
    facts = build_facts(spots, scores, conditions, errors, TZ)
    assert facts.entries
    message = message_from_facts(facts, spots, None)
    original = MessageStage(facts=facts, message=message)

    restored = deserialize_message_stage(
        through_json(serialize_message_stage(original))
    )

    assert restored == original
    assert restored.message is not None and restored.message.covered
    assert restored.facts.unchecked_spots == 1


def test_a_shortened_message_keeps_fewer_covered_keys_than_entries() -> None:
    message = Message(
        body="Today: ... +1 more",
        source="fallback",
        covered=(EntryKey("spot-0", "hiking", TODAY),),
    )
    stage = MessageStage(
        facts=dataclasses.replace(
            build_facts([], score_conditions([], THRESHOLDS, TZ), [], [], TZ)
        ),
        message=message,
    )

    restored = deserialize_message_stage(through_json(serialize_message_stage(stage)))

    assert restored.message == message


def test_nothing_to_send_is_a_message_of_none() -> None:
    facts = build_facts([], score_conditions([], THRESHOLDS, TZ), [], [], TZ)
    stage = MessageStage(facts=facts, message=None)

    restored = deserialize_message_stage(through_json(serialize_message_stage(stage)))

    assert restored.message is None and restored.facts == facts


def test_integers_in_float_fields_come_back_as_floats() -> None:
    document = through_json(serialize_fetch(a_fetch_result()))
    document["data"]["results"][0]["hourly"][0]["temperature_f"] = 60  # an int

    restored = deserialize_fetch(document)

    value = restored.results[0].hourly[0].temperature_f
    assert value == 60.0 and isinstance(value, float)


# --- time zones (AC6, AC17) ---


def dst_fall_back_conditions() -> SpotConditions:
    """Nov 1 2026: 01:30 happens twice in New York (EDT, then EST)."""
    first = datetime(2026, 11, 1, 1, 30, tzinfo=ET)
    second = datetime(2026, 11, 1, 1, 30, tzinfo=ET, fold=1)
    hours = tuple(
        HourlyConditions(time=t, **GOOD_HOUR)
        for t in (
            datetime(2026, 11, 1, 0, 30, tzinfo=ET),
            first,
            second,
            datetime(2026, 11, 1, 2, 30, tzinfo=ET),
        )
    )
    day = date(2026, 11, 1)
    window = DaylightWindow(
        day,
        datetime(2026, 11, 1, 6, 30, tzinfo=ET),
        datetime(2026, 11, 1, 16, 50, tzinfo=ET),
    )
    return SpotConditions(
        spot=make_spot("hiking"),
        fetched_at=datetime(2026, 11, 1, 0, 0, tzinfo=ET),
        hourly=hours,
        daylight=(window,),
        alerts=(),
    )


def test_both_1_30_am_hours_on_fall_back_day_keep_their_offsets_and_instants() -> None:
    original = FetchResult(results=[dst_fall_back_conditions()], errors=[])

    restored = deserialize_fetch(through_json(serialize_fetch(original)))

    assert instants(restored) == instants(original)
    offsets = [off for _, off in instants(restored.results[0].hourly)]
    assert offsets == [-4 * 3600, -4 * 3600, -5 * 3600, -5 * 3600]  # EDT, EDT, EST, EST
    # The two 01:30s are one hour apart, not equal.
    first, second = (
        restored.results[0].hourly[1].time,
        restored.results[0].hourly[2].time,
    )
    assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=1)


def test_serializing_again_after_a_round_trip_gives_the_same_document() -> None:
    original = FetchResult(results=[dst_fall_back_conditions()], errors=[])
    once = through_json(serialize_fetch(original))

    again = serialize_fetch(deserialize_fetch(once))

    assert through_json(again) == once


def test_scores_are_the_same_before_and_after_a_round_trip_across_dst() -> None:
    original = FetchResult(results=[dst_fall_back_conditions()], errors=[])
    restored = deserialize_fetch(through_json(serialize_fetch(original)))

    before = score_conditions(original.results, THRESHOLDS, TZ)
    after = score_conditions(restored.results, THRESHOLDS, TZ)

    assert [
        (s.activity, s.day, s.status) for r in before.results for s in r.scores
    ] == [(s.activity, s.day, s.status) for r in after.results for s in r.scores]
    assert [c for r in before.results for s in r.scores for c in s.checks] == [
        c for r in after.results for s in r.scores for c in s.checks
    ]


def test_the_stored_wall_clock_and_date_are_the_local_ones() -> None:
    restored = deserialize_fetch(
        through_json(serialize_fetch(FetchResult([build()], [])))
    )

    hour = restored.results[0].hourly[3].time
    assert (hour.hour, hour.date()) == (3, TODAY)
    assert hour.astimezone(ZoneInfo(TZ)).hour == 3


def test_a_naive_datetime_is_refused_on_write() -> None:
    naive = dataclasses.replace(build(), fetched_at=datetime(2026, 1, 15, 0, 30))

    with pytest.raises(SerializationError, match=r"fetch\.results\[0\]\.fetched_at"):
        serialize_fetch(FetchResult([naive], []))


def test_a_naive_datetime_is_refused_on_read() -> None:
    document = through_json(serialize_fetch(FetchResult([build()], [])))
    document["data"]["results"][0]["fetched_at"] = "2026-01-15T00:30:00"

    with pytest.raises(SerializationError, match="no naive times"):
        deserialize_fetch(document)


# --- bad data raises, never defaults (AC7) ---


def good_document() -> dict[str, Any]:
    return through_json(serialize_fetch(a_fetch_result()))


def test_a_missing_key_names_the_path_and_the_key() -> None:
    document = good_document()
    del document["data"]["results"][0]["spot"]["lat"]

    with pytest.raises(SerializationError, match=r"results\[0\]\.spot: missing lat"):
        deserialize_fetch(document)


def test_an_unknown_key_is_refused() -> None:
    document = good_document()
    document["data"]["errors"][0]["surprise"] = 1

    with pytest.raises(SerializationError, match="unknown surprise"):
        deserialize_fetch(document)


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        (("results", 0, "spot", "lat"), "40.0"),
        (("results", 0, "spot", "lat"), True),
        (("results", 0, "spot", "id"), 5),
        (("results", 0, "hourly", 0, "weather_code"), 1.5),
        (("results", 0, "hourly", 0, "weather_code"), False),
        (("results", 0, "spot", "activities"), "hiking"),
        (("results", 0, "spot", "activities", 0), "swimming"),
        (("errors", 0, "source"), "somewhere"),
        (("errors", 0, "status_code"), "500"),
        (("results", 0, "daylight", 0, "date"), "2026-1-5"),
        (("results", 0, "daylight", 0, "date"), "2026-01-15T00:00:00"),
        (("results", 0, "daylight", 0, "sunrise"), "yesterday"),
        (("results", 0, "daylight", 0, "sunrise"), 12),
        (("results", 0), []),
        (("results",), {}),
    ],
)
def test_wrong_types_and_values_raise_without_echoing_the_value(
    path: tuple[Any, ...], bad: Any
) -> None:
    document = good_document()
    target = document["data"]
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = bad

    with pytest.raises(SerializationError) as e:
        deserialize_fetch(document)

    assert "fetch." in str(e.value)
    assert str(bad) not in str(e.value) or str(bad) in ("True", "False", "{}", "[]")


def test_nan_and_infinity_are_refused_both_ways() -> None:
    for bad in (math.nan, math.inf):
        hour = HourlyConditions(
            time=at(TODAY, 3), **{**GOOD_HOUR, "temperature_f": bad}
        )
        conditions = dataclasses.replace(build(), hourly=(hour,))
        with pytest.raises(SerializationError, match="finite"):
            serialize_fetch(FetchResult([conditions], []))

    document = good_document()
    document["data"]["results"][0]["hourly"][0]["temperature_f"] = math.nan
    with pytest.raises(SerializationError, match="finite"):
        deserialize_fetch(document)


def test_truncated_or_empty_documents_raise() -> None:
    for bad in (None, [], {}, {"schema": "fetch"}, "text", 5):
        with pytest.raises(SerializationError):
            deserialize_fetch(bad)


def test_an_unknown_version_gets_its_own_error() -> None:
    document = good_document()
    document["version"] = 2

    with pytest.raises(UnsupportedSchemaVersionError, match="unsupported version"):
        deserialize_fetch(document)
    document["version"] = True
    with pytest.raises(UnsupportedSchemaVersionError):
        deserialize_fetch(document)


def test_one_stage_file_cannot_be_read_as_another() -> None:
    score_document = through_json(
        serialize_score(score_conditions([build()], THRESHOLDS, TZ))
    )

    with pytest.raises(SerializationError, match="wrong schema"):
        deserialize_fetch(score_document)


def test_serializing_the_wrong_type_or_schema_raises() -> None:
    with pytest.raises(SerializationError, match="expected FetchResult"):
        serialize("fetch", score_conditions([], THRESHOLDS, TZ))
    with pytest.raises(SerializationError, match="unknown schema"):
        serialize("nope", object())
    with pytest.raises(SerializationError, match="unknown schema"):
        deserialize("nope", {})


def test_a_literal_outside_its_values_is_refused_on_write() -> None:
    bad = SpotError("s", "somewhere", "x")  # type: ignore[arg-type]

    with pytest.raises(SerializationError, match="source"):
        serialize_fetch(FetchResult([], [bad]))
