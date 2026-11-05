import dataclasses
import logging

import pytest

from gooddays.models import Message, SendResult
from gooddays.notify import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    RECIPIENT_ENV,
    SendError,
    mask_number,
    parse_recipient,
    segment_count,
    send_notification,
)
from gooddays.storage import ConfigError

NUMBER = "+14155550123"
BODY = "Today Mon Jun 15:\nSpot 0 hiking: 65 F, wind 5 mph"
MESSAGE = Message(body=BODY, source="fallback")


class FakeSender:
    """Records calls; each call returns the next outcome (an id or an error)."""

    def __init__(self, *outcomes: str | SendError) -> None:
        self.outcomes = list(outcomes) or ["SM1"]
        self.calls: list[tuple[str, str]] = []

    def __call__(self, to: str, body: str) -> str:
        self.calls.append((to, body))
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, SendError):
            raise outcome
        return outcome


# --- the recipient ---


@pytest.mark.parametrize(
    "raw", [NUMBER, f"  {NUMBER}  ", "+442071838750", "+819012345678"]
)
def test_valid_recipients(raw: str) -> None:
    assert parse_recipient(raw) == raw.strip()


@pytest.mark.parametrize("raw", [None, "", "   ", "\n"])
def test_a_missing_recipient_names_the_setting(raw: str | None) -> None:
    with pytest.raises(ConfigError, match=RECIPIENT_ENV):
        parse_recipient(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "2125559876",  # no plus
        "+0155550123",  # a country code never starts with 0
        "+1212",  # too short
        "+1415555012345678",  # too long
        "+1 415 555 0123",  # spaces inside
        "+1415555O123",  # a letter O
        "tel:+14155550123",
        f"{NUMBER}\nextra",  # fullmatch: a trailing newline can't sneak through
    ],
)
def test_an_invalid_recipient_is_refused_without_echoing_it(raw: str) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_recipient(raw)

    assert RECIPIENT_ENV in str(exc.value)
    assert raw.strip() not in str(exc.value)


def test_masking_shows_only_the_last_four_digits() -> None:
    assert mask_number(NUMBER) == "+***0123"
    assert "415" not in mask_number(NUMBER)


@pytest.mark.parametrize(
    ("length", "segments"),
    [(1, 1), (160, 1), (161, 2), (306, 2), (307, 3), (459, 3), (460, 4)],
)
def test_segment_count(length: int, segments: int) -> None:
    assert segment_count("x" * length) == segments


# --- sending ---


def test_the_body_is_sent_once_unchanged() -> None:
    sender = FakeSender("SM123")

    result = send_notification(MESSAGE, NUMBER, sender)

    assert sender.calls == [(NUMBER, BODY)]
    assert result == SendResult(
        accepted=True, message_id="SM123", attempts=1, reason=None
    )


def test_nothing_to_send_never_calls_the_sender() -> None:
    sender = FakeSender()

    result = send_notification(None, NUMBER, sender)

    assert sender.calls == []
    assert result == SendResult(
        accepted=False, message_id=None, attempts=0, reason="nothing_to_send"
    )


def test_the_result_is_frozen() -> None:
    result = send_notification(MESSAGE, NUMBER, FakeSender())

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.accepted = False  # type: ignore[misc]


def test_a_retryable_failure_is_retried_once_after_a_short_wait(
    no_sms_sleep: list[float],
) -> None:
    sender = FakeSender(SendError("server_error", retryable=True), "SM9")

    result = send_notification(MESSAGE, NUMBER, sender)

    assert result.accepted and result.attempts == 2
    assert len(sender.calls) == 2
    assert no_sms_sleep == [BACKOFF_SECONDS]


def test_two_retryable_failures_give_up_after_the_allowed_attempts(
    no_sms_sleep: list[float],
) -> None:
    sender = FakeSender(SendError("server_error", retryable=True))

    with pytest.raises(SendError) as exc:
        send_notification(MESSAGE, NUMBER, sender)

    assert exc.value.reason == "server_error"
    assert exc.value.attempts == MAX_ATTEMPTS == 2
    assert len(sender.calls) == 2
    assert no_sms_sleep == [BACKOFF_SECONDS]  # no wait after the last attempt


@pytest.mark.parametrize(
    "reason",
    [
        "read_timeout",
        "rejected",
        "rejected_21211",
        "auth_rejected",
        "rate_limited",
        "bad_response",
    ],
)
def test_other_failures_are_never_retried(
    reason: str, no_sms_sleep: list[float]
) -> None:
    sender = FakeSender(SendError(reason))

    with pytest.raises(SendError) as exc:
        send_notification(MESSAGE, NUMBER, sender)

    assert exc.value.reason == reason and exc.value.attempts == 1
    assert len(sender.calls) == 1  # a retry could text twice
    assert no_sms_sleep == []


def test_logs_never_contain_the_body_or_the_full_number(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sender = FakeSender(SendError("server_error", retryable=True), "SM1")

    with caplog.at_level(logging.DEBUG):
        send_notification(MESSAGE, NUMBER, sender)

    assert "+***0123" in caplog.text
    assert NUMBER not in caplog.text and "5550123" not in caplog.text
    assert BODY not in caplog.text and "hiking" not in caplog.text
    assert (
        "sms_attempt attempt=1" in caplog.text and "sms_failed attempt=1" in caplog.text
    )


def test_send_error_keeps_only_the_reason_key() -> None:
    error = SendError("rejected_21211")

    assert str(error) == "rejected_21211"
    assert error.retryable is False
