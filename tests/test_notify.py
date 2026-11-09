import dataclasses
import logging

import pytest

from gooddays.models import Message, SendResult
from gooddays.notify import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    SUBJECT,
    TOPIC_ENV,
    SendError,
    mask_topic,
    parse_topic_arn,
    send_notification,
    topic_region,
)
from gooddays.storage import ConfigError

ARN = "arn:aws:sns:us-east-1:123456789012:good-days"
BODY = "Today Mon Jun 15:\nSpot 0 hiking: 65 F, wind 5 mph"
MESSAGE = Message(body=BODY, source="fallback")


class FakeSender:
    """Records calls; each call returns the next outcome (an id or an error)."""

    def __init__(self, *outcomes: str | SendError) -> None:
        self.outcomes = list(outcomes) or ["id-1"]
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, destination: str, subject: str, body: str) -> str:
        self.calls.append((destination, subject, body))
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, SendError):
            raise outcome
        return outcome


# --- the topic ARN ---


@pytest.mark.parametrize(
    "raw",
    [
        ARN,
        f"  {ARN}\n",
        "arn:aws:sns:eu-west-2:999999999999:a",
        "arn:aws-cn:sns:cn-north-1:123456789012:good_days-2",
        "arn:aws-us-gov:sns:us-gov-west-1:123456789012:t",
    ],
)
def test_valid_topic_arns(raw: str) -> None:
    assert parse_topic_arn(raw) == raw.strip()


@pytest.mark.parametrize("raw", [None, "", "   ", "\n"])
def test_a_missing_topic_names_the_setting(raw: str | None) -> None:
    with pytest.raises(ConfigError, match=TOPIC_ENV):
        parse_topic_arn(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "my-topic",  # a name, not an ARN
        "arn:aws:sns:us-east-1:12345:good-days",  # short account id
        "arn:aws:sns:us-west-2:210987654321:",  # no topic name
        "arn:aws:sqs:us-east-1:123456789012:good-days",  # the wrong service
        "arn:aws:sns::123456789012:good-days",  # no region
        "arn:aws:sns:us-east-1:123456789012:good days",  # a space
        f"{ARN}\nextra",  # fullmatch: a newline can't sneak through
        "https://example.com/arn",
    ],
)
def test_an_invalid_topic_is_refused_without_echoing_it(raw: str) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_topic_arn(raw)

    assert TOPIC_ENV in str(exc.value)
    assert raw.strip() not in str(exc.value)


def test_the_region_comes_from_the_arn() -> None:
    assert topic_region(ARN) == "us-east-1"
    assert topic_region("arn:aws:sns:eu-west-2:123456789012:t") == "eu-west-2"
    with pytest.raises(ValueError):
        topic_region("not an arn")


def test_masking_hides_all_but_the_last_four_digits_of_the_account() -> None:
    masked = mask_topic(ARN)

    assert masked == "arn:aws:sns:us-east-1:********9012:good-days"
    assert "123456789012" not in masked and "12345678" not in masked
    assert mask_topic("garbage") == "***"


def test_the_subject_follows_snss_email_rules() -> None:
    assert SUBJECT.isascii() and SUBJECT.isprintable()
    assert SUBJECT[0].isalnum() and SUBJECT == SUBJECT.strip()
    assert 0 < len(SUBJECT) < 100 and "\n" not in SUBJECT


# --- sending ---


def test_the_body_is_sent_once_unchanged_with_the_subject() -> None:
    sender = FakeSender("msg-123")

    result = send_notification(MESSAGE, ARN, sender)

    assert sender.calls == [(ARN, SUBJECT, BODY)]
    assert result == SendResult(
        accepted=True, message_id="msg-123", attempts=1, reason=None
    )


def test_nothing_to_send_never_calls_the_sender() -> None:
    sender = FakeSender()

    result = send_notification(None, ARN, sender)

    assert sender.calls == []
    assert result == SendResult(
        accepted=False, message_id=None, attempts=0, reason="nothing_to_send"
    )


def test_the_result_is_frozen() -> None:
    result = send_notification(MESSAGE, ARN, FakeSender())

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.accepted = False  # type: ignore[misc]


def test_a_retryable_failure_is_retried_once_after_a_short_wait(
    no_notify_sleep: list[float],
) -> None:
    sender = FakeSender(SendError("unavailable", retryable=True), "msg-9")

    result = send_notification(MESSAGE, ARN, sender)

    assert result.accepted and result.attempts == 2
    assert len(sender.calls) == 2
    assert no_notify_sleep == [BACKOFF_SECONDS]


def test_two_retryable_failures_give_up_after_the_allowed_attempts(
    no_notify_sleep: list[float],
) -> None:
    sender = FakeSender(SendError("unavailable", retryable=True))

    with pytest.raises(SendError) as exc:
        send_notification(MESSAGE, ARN, sender)

    assert exc.value.reason == "unavailable"
    assert exc.value.attempts == MAX_ATTEMPTS == 2
    assert len(sender.calls) == 2
    assert no_notify_sleep == [BACKOFF_SECONDS]  # no wait after the last attempt


@pytest.mark.parametrize(
    "reason",
    [
        "read_timeout",
        "rejected",
        "auth_rejected",
        "topic_not_found",
        "throttled",
        "no_credentials",
    ],
)
def test_other_failures_are_never_retried(
    reason: str, no_notify_sleep: list[float]
) -> None:
    sender = FakeSender(SendError(reason))

    with pytest.raises(SendError) as exc:
        send_notification(MESSAGE, ARN, sender)

    assert exc.value.reason == reason and exc.value.attempts == 1
    assert len(sender.calls) == 1  # a retry could notify twice
    assert no_notify_sleep == []


def test_logs_never_contain_the_body_or_the_full_account_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sender = FakeSender(SendError("unavailable", retryable=True), "msg-1")

    with caplog.at_level(logging.DEBUG):
        send_notification(MESSAGE, ARN, sender)

    assert "********9012" in caplog.text
    assert "123456789012" not in caplog.text
    assert BODY not in caplog.text and "hiking" not in caplog.text
    assert (
        "notify_attempt attempt=1" in caplog.text
        and "notify_failed attempt=1" in caplog.text
    )


def test_send_error_keeps_only_the_reason_key() -> None:
    error = SendError("rejected")

    assert str(error) == "rejected"
    assert error.retryable is False
