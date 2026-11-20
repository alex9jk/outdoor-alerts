"""The four stage handlers, with a fake store, fetchers, writer, sender and clock."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from gooddays import handlers
from gooddays.fetch.service import Fetchers
from gooddays.handlers import (
    AllSpotsFailedError,
    Dependencies,
    InvalidEventError,
    SendFailedError,
    SendOutcomeUnknownError,
    StageError,
)
from gooddays.models import EntryKey, Settings, Spot
from gooddays.notify import SUBJECT, TOPIC_ENV, SendError
from gooddays.objectstore import InMemoryObjectStore, ObjectNotFoundError
from gooddays.sent import LocalSentStore
from gooddays.serialize import SerializationError
from gooddays.storage import ConfigError, LocalStorage
from helpers import load_fixture
from test_cli import SPOTS_YAML, FakeSender

ET = ZoneInfo("America/New_York")
ARN = "arn:aws:sns:us-east-1:123456789012:good-days"
EXEC = "exec-2026-06-15-am"
KEY_TODAY = EntryKey("spot-0", "hiking", datetime(2026, 6, 15).date())
KEY_TOMORROW = EntryKey("spot-0", "hiking", datetime(2026, 6, 16).date())
START = {"execution_id": EXEC}


def clock() -> datetime:
    return datetime(2026, 6, 15, 4, 0, tzinfo=ET)


def good_fetchers() -> Fetchers:
    async def forecast(c: httpx.AsyncClient, spot: Spot, s: Settings) -> Any:
        return load_fixture("open_meteo_warm_dry.json")

    async def alerts(c: httpx.AsyncClient, spot: Spot, s: Settings) -> Any:
        return load_fixture("nws_alerts_none.json")

    return Fetchers(forecast=forecast, alerts=alerts)


def failing_fetchers() -> Fetchers:
    async def forecast(c: httpx.AsyncClient, spot: Spot, s: Settings) -> Any:
        raise httpx.ConnectError("down")

    return Fetchers(forecast=forecast, alerts=good_fetchers().alerts)


def only_spot_1_fails() -> Fetchers:
    good = good_fetchers()

    async def forecast(c: httpx.AsyncClient, spot: Spot, s: Settings) -> Any:
        if spot.id == "spot-1":
            raise httpx.ConnectError("down")
        return await good.forecast(c, spot, s)

    return Fetchers(forecast=forecast, alerts=good.alerts)


class Writer:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, facts: str, max_chars: int) -> str:
        self.calls += 1
        raise AssertionError("this test does not expect Claude to be called")


@pytest.fixture
def parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    (tmp_path / "spots.yaml").write_text(SPOTS_YAML)
    monkeypatch.setenv(TOPIC_ENV, ARN)
    monkeypatch.setenv("GOODDAYS_NWS_USER_AGENT", "gooddays-test (t@example.com)")
    return {
        "store": InMemoryObjectStore(),
        "storage": LocalStorage(tmp_path / "spots.yaml"),
        "sent_store": LocalSentStore(tmp_path / "sent" / "sent.json", lock_wait=0.3),
        "sender": FakeSender("msg-1"),
        "clock": clock,
    }


def deps(parts: dict[str, Any], **override: Any) -> Dependencies:
    return Dependencies(**{"fetchers": good_fetchers(), **parts, **override})


def run_to_message(parts: dict[str, Any], **override: Any) -> dict[str, Any]:
    d = deps(parts, **override)
    event = handlers.fetch(START, None, d)
    event = handlers.score(event, None, d)
    return handlers.message(event, None, d)


def run_all(parts: dict[str, Any], **override: Any) -> dict[str, Any]:
    event = run_to_message(parts, **override)
    return handlers.send(event, None, deps(parts, **override))


# --- fetch ---


def test_fetch_writes_the_result_and_returns_only_keys_and_counts(parts: Any) -> None:
    event = handlers.fetch(START, None, deps(parts))

    assert event == {
        "execution_id": EXEC,
        "fetch_key": f"runs/{EXEC}/fetch.json",
        "spots_ok": 2,
        "spots_failed": 0,
    }
    assert parts["store"].keys() == [f"runs/{EXEC}/fetch.json"]


def test_a_failing_spot_is_recorded_and_the_run_continues(parts: Any) -> None:
    event = handlers.fetch(START, None, deps(parts, fetchers=only_spot_1_fails()))

    assert (event["spots_ok"], event["spots_failed"]) == (1, 1)
    stored = parts["store"].get_json(event["fetch_key"])
    assert stored["data"]["errors"][0]["spot_id"] == "spot-1"


def test_every_spot_failing_raises_after_writing_the_file(parts: Any) -> None:
    with pytest.raises(AllSpotsFailedError, match="every spot failed"):
        handlers.fetch(START, None, deps(parts, fetchers=failing_fetchers()))

    assert parts["store"].keys() == [f"runs/{EXEC}/fetch.json"]


# --- score ---


def test_score_reads_fetch_and_writes_its_own_file(parts: Any) -> None:
    d = deps(parts)
    event = handlers.score(handlers.fetch(START, None, d), None, d)

    assert event["score_key"] == f"runs/{EXEC}/score.json"
    assert event["fetch_key"] == f"runs/{EXEC}/fetch.json"
    assert event["spots_ok"] == 2  # earlier flags are carried along
    assert f"runs/{EXEC}/score.json" in parts["store"].keys()


def test_score_without_a_fetch_file_names_the_missing_key(parts: Any) -> None:
    with pytest.raises(ObjectNotFoundError, match=f"runs/{EXEC}/fetch.json"):
        handlers.score(START, None, deps(parts))


def test_score_with_invalid_thresholds_raises_a_config_error(
    parts: Any, tmp_path: Path
) -> None:
    d = deps(parts)
    event = handlers.fetch(START, None, d)
    (tmp_path / "spots.yaml").write_text(
        SPOTS_YAML.replace("max_rain_in: 0", "max_rain_in: -1")
    )

    with pytest.raises(ConfigError, match="max_rain_in"):
        handlers.score(event, None, d)


# --- message ---


def test_message_filters_sent_entries_and_writes_facts_and_text(parts: Any) -> None:
    d = deps(parts, writer=lambda facts, cap: _spot0_text())
    event = handlers.message(
        handlers.score(handlers.fetch(START, None, d), None, d), None, d
    )

    assert event["nothing_to_send"] is False
    stored = parts["store"].get_json(event["message_key"])
    assert stored["data"]["message"]["covered"]  # the keys Send will record
    assert len(stored["data"]["facts"]["entries"]) == 2


def _spot0_text() -> str:
    return (
        "Today:\nSpot 0 hiking: 65 F, wind 5 mph, daylight 5:25-20:35\n"
        "Tomorrow:\nSpot 0 hiking: 65 F, wind 5 mph, daylight 5:25-20:36"
    )


def test_message_skips_entries_already_sent(parts: Any) -> None:
    parts["sent_store"].record([KEY_TODAY], clock(), "old", clock().date())
    d = deps(parts, writer=lambda facts, cap: _tomorrow_text())
    event = handlers.message(
        handlers.score(handlers.fetch(START, None, d), None, d), None, d
    )

    stored = parts["store"].get_json(event["message_key"])
    entries = stored["data"]["facts"]["entries"]
    assert [e["day"] for e in entries] == ["2026-06-16"]


def _tomorrow_text() -> str:
    return "Tomorrow:\nSpot 0 hiking: 65 F, wind 5 mph, daylight 5:25-20:36"


def test_nothing_new_means_no_claude_call_and_a_flag(parts: Any) -> None:
    parts["sent_store"].record(
        [KEY_TODAY, KEY_TOMORROW], clock(), "old", clock().date()
    )
    writer = Writer()

    event = run_to_message(parts, writer=writer)

    assert event["nothing_to_send"] is True and "message_key" in event
    assert writer.calls == 0
    stored = parts["store"].get_json(event["message_key"])
    assert stored["data"]["message"] is None


def test_message_without_score_file_names_the_missing_key(parts: Any) -> None:
    d = deps(parts)
    event = handlers.fetch(START, None, d)

    with pytest.raises(ObjectNotFoundError, match=f"runs/{EXEC}/score.json"):
        handlers.message(event, None, d)


def test_a_corrupt_sent_record_fails_the_message_stage(parts: Any) -> None:
    d = deps(parts, writer=Writer())
    event = handlers.score(handlers.fetch(START, None, d), None, d)
    path = parts["sent_store"].path
    path.parent.mkdir(parents=True)
    path.write_text("{oops")

    with pytest.raises(ConfigError, match="sent record"):
        handlers.message(event, None, d)


# --- send ---


def with_message(parts: Any) -> dict[str, Any]:
    d = deps(parts, writer=lambda facts, cap: _spot0_text())
    event = handlers.fetch(START, None, d)
    event = handlers.score(event, None, d)
    return handlers.message(event, None, d)


def test_send_publishes_once_and_records_only_what_the_text_names(parts: Any) -> None:
    event = with_message(parts)

    result = handlers.send(event, None, deps(parts))

    assert result["sent"] is True and result["message_id"] == "msg-1"
    assert result["recorded"] is True and result["attempts"] == 1
    (call,) = parts["sender"].calls
    assert call[0] == ARN and call[1] == SUBJECT and "Spot 0 hiking" in call[2]
    assert parts["sent_store"].sent_keys() == {KEY_TODAY, KEY_TOMORROW}


def test_nothing_to_send_touches_nothing(
    parts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Exploding:
        def get_json(self, key: str) -> Any:
            raise AssertionError("no read expected")

        def put_json(self, key: str, data: Any) -> None:
            raise AssertionError("no write expected")

    monkeypatch.delenv(TOPIC_ENV)
    sender = FakeSender()

    result = handlers.send(
        {"execution_id": EXEC, "nothing_to_send": True},
        None,
        Dependencies(store=Exploding(), sender=sender),
    )

    assert result["sent"] is False and sender.calls == []


def test_send_needs_a_clear_nothing_to_send_flag(parts: Any) -> None:
    for bad in ({}, {"nothing_to_send": "yes"}, {"nothing_to_send": None}):
        with pytest.raises(InvalidEventError, match="nothing_to_send"):
            handlers.send({"execution_id": EXEC, **bad}, None, deps(parts))


def test_a_retryable_failure_calls_the_sender_at_most_twice(parts: Any) -> None:
    sender = FakeSender(SendError("unavailable", retryable=True))

    with pytest.raises(SendFailedError) as e:
        handlers.send(with_message(parts), None, deps(parts, sender=sender))

    assert len(sender.calls) == 2
    assert (e.value.reason, e.value.attempts) == ("unavailable", 2)
    assert parts["sent_store"].sent_keys() == frozenset()


def test_a_failed_send_records_nothing_and_leaves_the_message_in_the_store(
    parts: Any,
) -> None:
    event = with_message(parts)

    with pytest.raises(SendFailedError, match="auth_rejected"):
        handlers.send(
            event, None, deps(parts, sender=FakeSender(SendError("auth_rejected")))
        )

    assert parts["sent_store"].sent_keys() == frozenset()
    assert event["message_key"] in parts["store"].keys()


def test_a_read_timeout_is_an_unknown_outcome_and_records_nothing(parts: Any) -> None:
    event = with_message(parts)

    with pytest.raises(SendOutcomeUnknownError, match="may still have been delivered"):
        handlers.send(
            event, None, deps(parts, sender=FakeSender(SendError("read_timeout")))
        )

    assert parts["sent_store"].sent_keys() == frozenset()


@pytest.mark.parametrize("reason", ["no_credentials", "bad_aws_config"])
def test_aws_setup_problems_are_config_errors(parts: Any, reason: str) -> None:
    with pytest.raises(ConfigError, match="nothing was sent"):
        handlers.send(
            with_message(parts), None, deps(parts, sender=FakeSender(SendError(reason)))
        )


def test_a_missing_or_invalid_topic_is_a_config_error_naming_the_setting(
    parts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = with_message(parts)
    monkeypatch.delenv(TOPIC_ENV)

    with pytest.raises(ConfigError, match=TOPIC_ENV):
        handlers.send(event, None, deps(parts))

    assert parts["sender"].calls == []


def test_a_record_failure_after_the_publish_warns_and_does_not_raise(
    parts: Any, caplog: pytest.LogCaptureFixture
) -> None:
    event = with_message(parts)

    class BrokenRecord(LocalSentStore):
        def record(self, *args: Any, **kwargs: Any) -> None:
            raise OSError("disk full")

    broken = BrokenRecord(parts["sent_store"].path)
    with caplog.at_level(logging.WARNING):
        result = handlers.send(event, None, deps(parts, sent_store=broken))

    assert result["sent"] is True and result["recorded"] is False
    assert "sent_record_failed" in caplog.text


def test_send_with_no_message_file_names_the_missing_key(parts: Any) -> None:
    with pytest.raises(ObjectNotFoundError, match=f"runs/{EXEC}/message.json"):
        handlers.send(
            {"execution_id": EXEC, "nothing_to_send": False}, None, deps(parts)
        )


def test_send_refuses_an_empty_message_when_the_flag_says_there_is_one(
    parts: Any,
) -> None:
    event = run_to_message(parts, writer=Writer(), sent_store=_all_sent(parts))
    assert event["nothing_to_send"] is True
    event = {**event, "nothing_to_send": False}

    with pytest.raises(InvalidEventError, match="no message"):
        handlers.send(event, None, deps(parts))


def _all_sent(parts: Any) -> LocalSentStore:
    parts["sent_store"].record(
        [KEY_TODAY, KEY_TOMORROW], clock(), "old", clock().date()
    )
    return parts["sent_store"]


def test_a_stage_file_of_the_wrong_kind_is_a_serialization_error(parts: Any) -> None:
    d = deps(parts)
    event = handlers.fetch(START, None, d)
    parts["store"].put_json(
        f"runs/{EXEC}/score.json", parts["store"].get_json(event["fetch_key"])
    )

    with pytest.raises(SerializationError, match="wrong schema"):
        handlers.message({**event, "score_key": f"runs/{EXEC}/score.json"}, None, d)


# --- events and ids ---


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"execution_id": ""},
        {"execution_id": 5},
        {"execution_id": "a/b"},
        {"execution_id": "a..b"},
        {"execution_id": "a\\b"},
        {"execution_id": "a\nb"},
    ],
)
@pytest.mark.parametrize("stage", ["fetch", "score", "message", "send"])
def test_a_bad_execution_id_is_refused_by_every_stage(
    parts: Any, stage: str, event: dict[str, Any]
) -> None:
    with pytest.raises(InvalidEventError, match="execution_id"):
        getattr(handlers, stage)(event, None, deps(parts))


def test_a_key_that_belongs_to_another_run_is_refused(parts: Any) -> None:
    d = deps(parts)
    event = handlers.fetch(START, None, d)

    with pytest.raises(InvalidEventError, match="fetch_key"):
        handlers.score({**event, "fetch_key": "runs/other/fetch.json"}, None, d)


def test_the_same_execution_id_twice_overwrites_the_run(parts: Any) -> None:
    d = deps(parts)
    handlers.fetch(START, None, d)
    handlers.fetch(START, None, d)

    assert parts["store"].keys() == [f"runs/{EXEC}/fetch.json"]


def test_exception_class_names_are_stable_for_the_state_machine() -> None:
    assert [
        cls.__name__
        for cls in (
            StageError,
            InvalidEventError,
            AllSpotsFailedError,
            SendFailedError,
            SendOutcomeUnknownError,
        )
    ] == [
        "StageError",
        "InvalidEventError",
        "AllSpotsFailedError",
        "SendFailedError",
        "SendOutcomeUnknownError",
    ]
    assert not issubclass(SendFailedError, SendOutcomeUnknownError)
    assert not issubclass(SendOutcomeUnknownError, SendFailedError)


# --- size and secrets ---


def test_returned_events_stay_tiny_however_big_the_run(parts: Any) -> None:
    import json

    event = with_message(parts)

    stored = len(json.dumps(parts["store"].get_json(event["fetch_key"])))
    assert stored > 10_000
    assert len(json.dumps(event)) < 1_000  # the state machine limit is 256 KB


def test_logs_and_errors_never_hold_the_topic_the_key_or_the_body(
    parts: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-KEY-VALUE")
    event = with_message(parts)
    with caplog.at_level(logging.DEBUG):
        handlers.send(event, None, deps(parts))
        with pytest.raises(SendFailedError) as failed:
            handlers.send(
                event, None, deps(parts, sender=FakeSender(SendError("auth_rejected")))
            )

    everything = caplog.text + str(failed.value)
    assert "123456789012" not in everything
    assert "sk-ant-SECRET" not in everything
    assert "daylight 5:25" not in everything  # the body
    assert "********9012" in everything  # the masked topic is fine


def test_importing_the_handlers_loads_neither_boto3_nor_the_claude_sdk() -> None:
    import subprocess
    import sys

    code = (
        "import sys, gooddays.handlers;"
        " assert 'boto3' not in sys.modules and 'anthropic' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
