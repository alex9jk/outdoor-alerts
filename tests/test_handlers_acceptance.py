"""Acceptance gaps for the stage handlers (phase 1 of the AWS move).

These add to test_handlers.py and test_pipeline_local.py: stage-file envelopes, every
stage's key check, the default dependencies built from the environment, and secrets in
the failure paths.
"""

# The `parts` fixture is imported from test_handlers.
# ruff: noqa: F811

import json
import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest

from gooddays import ai_writer, handlers, sns_sender
from gooddays.handlers import (
    BUCKET_ENV,
    SENT_PATH_ENV,
    SPOTS_PATH_ENV,
    Dependencies,
    InvalidEventError,
    SendFailedError,
    SendOutcomeUnknownError,
)
from gooddays.notify import AWS_SETUP_PROBLEMS, TOPIC_ENV, SendError
from gooddays.sent import LocalSentStore
from gooddays.storage import ConfigError
from test_cli import SPOTS_YAML, FakeSender
from test_handlers import (
    EXEC,
    START,
    Writer,
    _spot0_text,
    deps,
    good_fetchers,
    parts,  # noqa: F401  (a fixture)
    with_message,
)

# --- criterion 3: the files the handlers write are {"schema", "version": 1, "data"} ---


def test_every_stage_file_is_a_versioned_envelope(parts: Any) -> None:
    event = with_message(parts)

    for key in (event["fetch_key"], event["score_key"], event["message_key"]):
        document = parts["store"].get_json(key)
        assert set(document) == {"schema", "version", "data"}
        assert document["version"] == 1
        assert isinstance(document["schema"], str) and document["schema"]
    schemas = {
        parts["store"].get_json(event[k])["schema"]
        for k in ("fetch_key", "score_key", "message_key")
    }
    assert len(schemas) == 3  # one schema per stage file


def test_a_stage_file_with_an_unknown_version_is_refused_by_the_next_stage(
    parts: Any,
) -> None:
    from gooddays.serialize import SerializationError

    d = deps(parts)
    event = handlers.fetch(START, None, d)
    document = parts["store"].get_json(event["fetch_key"])
    parts["store"].put_json(event["fetch_key"], {**document, "version": 2})

    with pytest.raises(SerializationError):
        handlers.score(event, None, d)


# --- criterion 1 and 2: each stage derives and checks its own keys ---


@pytest.mark.parametrize(
    ("stage", "bad_key"),
    [
        ("score", "fetch_key"),
        ("score", "score_key"),
        ("message", "fetch_key"),
        ("message", "score_key"),
        ("message", "message_key"),
        ("send", "message_key"),
    ],
)
def test_every_stage_refuses_a_key_from_another_run(
    parts: Any, stage: str, bad_key: str
) -> None:
    event = with_message(parts)
    event = {**event, "nothing_to_send": False, bad_key: "runs/other/x.json"}
    sender = FakeSender()

    with pytest.raises(InvalidEventError, match=bad_key):
        getattr(handlers, stage)(event, None, deps(parts, sender=sender))

    assert sender.calls == []


def test_a_key_that_only_looks_like_the_derived_one_is_refused(parts: Any) -> None:
    d = deps(parts)
    event = handlers.fetch(START, None, d)

    for sneaky in (
        f"runs/{EXEC}/fetch.json ",
        f"runs/{EXEC}/../{EXEC}/fetch.json",
        f"/runs/{EXEC}/fetch.json",
        5,
        "",
    ):
        with pytest.raises(InvalidEventError, match="fetch_key"):
            handlers.score({**event, "fetch_key": sneaky}, None, d)


@pytest.mark.parametrize(
    "bad",
    [
        "a\tb",
        "a\rb",
        "a\x00b",
        "a\x1fb",
        "a\x7fb",
        "a\x85b",
        "a\u2028b",
        ".",
        "..",
        "a/..",
        " /",
        "x" * 81,
    ],
)
def test_more_unsafe_execution_ids_are_refused(parts: Any, bad: str) -> None:
    with pytest.raises(InvalidEventError, match="execution_id"):
        handlers.fetch({"execution_id": bad}, None, deps(parts))

    assert parts["store"].keys() == []


@pytest.mark.parametrize("bad", [None, 5, 1.5, ["x"], {"a": "b"}, True])
def test_a_non_string_execution_id_is_refused(parts: Any, bad: Any) -> None:
    with pytest.raises(InvalidEventError, match="execution_id"):
        handlers.fetch({"execution_id": bad}, None, deps(parts))


def test_stages_accept_a_lambda_context_object_and_return_plain_json(
    parts: Any,
) -> None:
    class Context:  # what AWS passes; the handlers must not need anything from it
        aws_request_id = "r-1"

    d = deps(parts, writer=lambda facts, cap: _spot0_text())
    event: dict[str, Any] = dict(START)
    for stage in (handlers.fetch, handlers.score, handlers.message, handlers.send):
        event = stage(event, Context(), d)
        assert json.loads(json.dumps(event)) == event  # JSON-safe, nothing else in it

    assert event["sent"] is True


def test_a_stage_does_not_mutate_the_event_it_was_given(parts: Any) -> None:
    d = deps(parts)
    event = {"execution_id": EXEC}

    handlers.fetch(event, None, d)

    assert event == {"execution_id": EXEC}


# --- criterion 4 ---


def test_a_partial_failure_keeps_the_error_in_the_file_and_the_count_in_the_event(
    parts: Any,
) -> None:
    from test_handlers import only_spot_1_fails

    d = deps(parts, fetchers=only_spot_1_fails())
    event = handlers.fetch(START, None, d)
    event = handlers.score(event, None, d)

    assert (event["spots_ok"], event["spots_failed"]) == (1, 1)  # carried to score
    assert [
        e["spot_id"]
        for e in parts["store"].get_json(event["fetch_key"])["data"]["errors"]
    ] == ["spot-1"]


# --- criterion 6: send ---


@pytest.mark.parametrize("reason", sorted(AWS_SETUP_PROBLEMS))
def test_every_setup_problem_is_a_config_error_after_one_attempt(
    parts: Any, reason: str
) -> None:
    sender = FakeSender(SendError(reason))

    with pytest.raises(ConfigError, match="nothing was sent") as e:
        handlers.send(with_message(parts), None, deps(parts, sender=sender))

    assert len(sender.calls) == 1
    assert not isinstance(e.value, SendFailedError)
    assert parts["sent_store"].sent_keys() == frozenset()


@pytest.mark.parametrize("reason", ["throttled", "rejected", "topic_not_found"])
def test_a_non_retryable_failure_is_tried_once_and_reports_the_reason(
    parts: Any, reason: str
) -> None:
    sender = FakeSender(SendError(reason))

    with pytest.raises(SendFailedError) as e:
        handlers.send(with_message(parts), None, deps(parts, sender=sender))

    assert len(sender.calls) == 1
    assert (e.value.reason, e.value.attempts) == (reason, 1)
    assert parts["sent_store"].sent_keys() == frozenset()


def test_a_read_timeout_raises_unknown_outcome_whatever_the_retry_flag(
    parts: Any,
) -> None:
    sender = FakeSender(SendError("read_timeout", retryable=True))

    with pytest.raises(SendOutcomeUnknownError) as e:
        handlers.send(with_message(parts), None, deps(parts, sender=sender))

    assert e.value.attempts == len(sender.calls)
    assert len(sender.calls) <= 2
    assert not isinstance(e.value, SendFailedError)


def test_a_retry_that_then_succeeds_records_once_with_two_attempts(
    parts: Any,
) -> None:
    sender = FakeSender(SendError("unavailable", retryable=True), "msg-9")

    result = handlers.send(with_message(parts), None, deps(parts, sender=sender))

    assert len(sender.calls) == 2
    assert (result["attempts"], result["message_id"]) == (2, "msg-9")
    assert result["recorded"] is True
    rows = json.loads(parts["sent_store"].path.read_text())["entries"]
    assert {r["message_id"] for r in rows} == {"msg-9"} and len(rows) == 2


def test_nothing_is_recorded_before_the_publish_is_accepted(parts: Any) -> None:
    event = with_message(parts)
    seen: list[frozenset[Any]] = []

    def sender(destination: str, subject: str, body: str) -> str:
        seen.append(parts["sent_store"].sent_keys())
        return "msg-1"

    handlers.send(event, None, deps(parts, sender=sender))

    assert seen == [frozenset()]
    assert len(parts["sent_store"].sent_keys()) == 2


def test_a_lock_failure_after_an_accepted_publish_is_a_warning_not_an_error(
    parts: Any, caplog: pytest.LogCaptureFixture
) -> None:
    class LockTimesOut(LocalSentStore):
        def lock(self) -> AbstractContextManager[None]:
            @contextmanager
            def busy() -> Iterator[None]:
                raise ConfigError("another run holds /secret/path/sent.json.lock")
                yield

            return busy()

    event = with_message(parts)
    stuck = LockTimesOut(parts["sent_store"].path)

    with caplog.at_level(logging.DEBUG):
        result = handlers.send(event, None, deps(parts, sent_store=stuck))

    assert result["sent"] is True and result["recorded"] is False
    assert "sent_record_failed" in caplog.text
    assert "/secret/path" not in caplog.text


def test_one_send_call_publishes_exactly_once(
    parts: Any,
) -> None:
    # Each call is one publish: the handler itself never loops over the sender.
    event = with_message(parts)
    sender = FakeSender()

    handlers.send(event, None, deps(parts, sender=sender))

    assert len(sender.calls) == 1


# --- criterion 5: nothing_to_send ---


def test_message_with_nothing_new_makes_no_sender_or_topic_demand(
    parts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_handlers import _all_sent

    monkeypatch.delenv(TOPIC_ENV)
    writer = Writer()
    sent = _all_sent(parts)
    d = deps(parts, writer=writer, sent_store=sent)
    event = handlers.score(handlers.fetch(START, None, d), None, d)

    event = handlers.message(event, None, d)
    result = handlers.send(event, None, d)

    assert event["nothing_to_send"] is True and writer.calls == 0
    assert result["sent"] is False
    assert parts["sender"].calls == []
    # the empty message file still exists, for the audit trail
    assert parts["store"].get_json(event["message_key"])["data"]["message"] is None


def test_send_with_nothing_to_send_ignores_a_bad_message_key_and_broken_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not be built")

    monkeypatch.setattr(sns_sender, "sns_email_sender", forbidden)
    monkeypatch.setattr(ai_writer, "anthropic_writer", forbidden)
    monkeypatch.delenv(TOPIC_ENV, raising=False)

    result = handlers.send({"execution_id": EXEC, "nothing_to_send": True})

    assert result["sent"] is False


# --- criterion 7: default dependencies come from the environment, lazily ---


def test_no_bucket_is_a_config_error_naming_the_setting(
    parts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(BUCKET_ENV, raising=False)
    d = Dependencies(storage=parts["storage"], fetchers=good_fetchers())

    with pytest.raises(ConfigError, match=BUCKET_ENV):
        handlers.fetch(START, None, d)


def test_a_bucket_from_the_environment_reaches_the_s3_store_not_a_real_client(
    parts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BUCKET_ENV, "my-private-bucket")
    d = Dependencies(storage=parts["storage"], fetchers=good_fetchers())

    # conftest makes the real S3 client impossible; this proves the store is S3's.
    with pytest.raises(AssertionError, match="real S3 client"):
        handlers.fetch(START, None, d)


def test_the_spots_and_sent_paths_come_from_the_environment(
    parts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "my-spots.yaml").write_text(SPOTS_YAML)
    sent_path = other / "my-sent.json"
    monkeypatch.setenv(SPOTS_PATH_ENV, str(other / "my-spots.yaml"))
    monkeypatch.setenv(SENT_PATH_ENV, str(sent_path))
    d = Dependencies(
        store=parts["store"],
        fetchers=good_fetchers(),
        writer=lambda facts, cap: _spot0_text(),
        sender=FakeSender(),
        clock=parts["clock"],
    )

    event: dict[str, Any] = dict(START)
    for stage in (handlers.fetch, handlers.score, handlers.message, handlers.send):
        event = stage(event, None, d)

    assert event["sent"] is True and event["recorded"] is True
    assert len(json.loads(sent_path.read_text())["entries"]) == 2


def test_a_missing_spots_file_from_the_environment_is_a_config_error(
    parts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SPOTS_PATH_ENV, str(tmp_path / "nope.yaml"))
    d = Dependencies(store=parts["store"], fetchers=good_fetchers())

    with pytest.raises(ConfigError):
        handlers.fetch(START, None, d)


def test_a_missing_writer_or_sender_is_built_from_the_real_factories(
    parts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []
    sender = FakeSender()

    def make_writer() -> Any:
        built.append("writer")
        return lambda facts, cap: _spot0_text()

    def make_sender() -> Any:
        built.append("sender")
        return sender

    monkeypatch.setattr(ai_writer, "anthropic_writer", make_writer)
    monkeypatch.setattr(sns_sender, "sns_email_sender", make_sender)
    d = Dependencies(
        store=parts["store"],
        storage=parts["storage"],
        fetchers=good_fetchers(),
        sent_store=parts["sent_store"],
        clock=parts["clock"],
    )

    event: dict[str, Any] = dict(START)
    for stage in (handlers.fetch, handlers.score):
        event = stage(event, None, d)
    assert built == []  # fetch and score never build either
    event = handlers.message(event, None, d)
    assert built == ["writer"]
    handlers.send(event, None, d)

    assert built == ["writer", "sender"] and len(sender.calls) == 1


def test_importing_handlers_does_not_load_the_sns_or_s3_modules() -> None:
    import subprocess
    import sys

    code = (
        "import sys, gooddays.handlers;"
        " bad = [m for m in ('gooddays.s3_store', 'gooddays.sns_sender',"
        " 'gooddays.ai_writer', 'botocore', 'anthropic') if m in sys.modules];"
        " assert not bad, bad"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


# --- criterion 8: secrets stay out of logs and errors, on every failure path ---


@pytest.mark.parametrize(
    "outcome",
    [
        SendError("read_timeout"),
        SendError("throttled"),
        SendError("unavailable", retryable=True),
        SendError("no_credentials"),
    ],
)
def test_no_failure_path_leaks_the_topic_the_key_or_the_body(
    parts: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    outcome: SendError,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-KEY-VALUE")
    # an account id unlike the example in the error help text
    monkeypatch.setenv(TOPIC_ENV, "arn:aws:sns:us-east-1:999988887777:good-days")
    event = with_message(parts)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as raised:
            handlers.send(event, None, deps(parts, sender=FakeSender(outcome)))

    everything = caplog.text + str(raised.value) + repr(raised.value)
    assert "999988887777" not in everything
    assert "sk-ant-SECRET" not in everything
    assert "daylight 5:25" not in everything


def test_a_bad_topic_error_does_not_echo_the_value(
    parts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = with_message(parts)
    monkeypatch.setenv(TOPIC_ENV, "not-an-arn-SECRETVALUE-999988887777")

    with pytest.raises(ConfigError) as e:
        handlers.send(event, None, deps(parts))

    assert TOPIC_ENV in str(e.value)
    assert "SECRETVALUE" not in str(e.value) and "999988887777" not in str(e.value)


def test_the_non_send_stages_log_no_secrets_and_no_text(
    parts: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-KEY-VALUE")
    with caplog.at_level(logging.DEBUG):
        with_message(parts)

    assert "sk-ant-SECRET" not in caplog.text
    assert "daylight 5:25" not in caplog.text
    assert "123456789012" not in caplog.text


def test_the_autouse_guards_make_real_aws_clients_impossible(parts: Any) -> None:
    from gooddays import s3_store

    with pytest.raises(AssertionError, match="real S3 client"):
        s3_store._new_client()
    with pytest.raises(AssertionError, match="real AWS client"):
        sns_sender._new_client("us-east-1")


def test_a_settings_failure_after_an_accepted_publish_only_warns(
    parts: Any, caplog: pytest.LogCaptureFixture
) -> None:
    event = with_message(parts)
    real = parts["storage"]
    sender = parts["sender"]

    class SettingsBreakAfterPublish:
        def get_settings(self) -> Any:
            if sender.calls:
                raise ConfigError("GOODDAYS_NWS_USER_AGENT is not set")
            return real.get_settings()

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

    caplog.set_level(logging.WARNING)

    result = handlers.send(
        event, None, deps(parts, storage=SettingsBreakAfterPublish())
    )

    assert result["sent"] is True and result["recorded"] is False
    assert "sent_record_failed" in caplog.text
    assert len(sender.calls) == 1


# --- the sent record must be writable before anything irreversible happens ---


def unwritable(parts: Any, tmp_path: Path) -> LocalSentStore:
    (tmp_path / "blocker").write_text("a file, so nothing can be created below it")
    return LocalSentStore(tmp_path / "blocker" / "sent.json", lock_wait=0.3)


def test_message_refuses_before_claude_when_the_record_cannot_be_written(
    parts: Any, tmp_path: Path
) -> None:
    writer = Writer()
    d = deps(parts, writer=writer, sent_store=unwritable(parts, tmp_path))
    event = handlers.score(handlers.fetch(START, None, d), None, d)

    with pytest.raises(ConfigError, match="sent record.*GOODDAYS_SENT_PATH"):
        handlers.message(event, None, d)

    assert writer.calls == 0
    assert "runs/exec-1/message.json" not in parts["store"].keys()


def test_send_refuses_before_publishing_when_the_record_cannot_be_written(
    parts: Any, tmp_path: Path
) -> None:
    event = with_message(parts)  # written while the record was still fine

    with pytest.raises(ConfigError, match="nothing was sent"):
        handlers.send(event, None, deps(parts, sent_store=unwritable(parts, tmp_path)))

    assert parts["sender"].calls == []


def test_nothing_to_send_does_not_check_the_record(parts: Any, tmp_path: Path) -> None:
    d = deps(parts, sent_store=unwritable(parts, tmp_path))

    result = handlers.send({"execution_id": EXEC, "nothing_to_send": True}, None, d)

    assert result["sent"] is False
    assert (tmp_path / "blocker").read_text().startswith("a file")
