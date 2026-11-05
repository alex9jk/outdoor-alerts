"""send_notification(): hand the message to an SMS sender, with a narrow retry.

Pure apart from the injected `sender` (which is the only thing that talks to a
provider) and `_sleep`. A text message is not idempotent: a retry after the
request may have left the machine could text twice, so only failures that
clearly happened before the provider took the request are retried.
"""

import logging
import math
import re
import time
from collections.abc import Callable

from gooddays.models import Message, SendResult
from gooddays.storage import ConfigError

logger = logging.getLogger(__name__)

RECIPIENT_ENV = "GOODDAYS_SMS_TO"
MAX_ATTEMPTS = 2  # in total, including the first
BACKOFF_SECONDS = 1.0

# Indirection so tests can skip the real wait.
_sleep = time.sleep

# International format: a plus, a non-zero first digit, 7 to 15 digits in all.
_E164 = re.compile(r"\+[1-9]\d{6,14}")

# (recipient, body) -> the provider's message id. Raises SendError.
SmsSender = Callable[[str, str], str]


class SendError(Exception):
    """A send failed. `reason` is a short key that is safe to log and print.

    `retryable` is True only when the request clearly did not reach the provider
    or the provider reported its own failure (5xx). Never put provider response
    text in here: it can echo the phone number.
    """

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.attempts = 0  # filled in by send_notification


def parse_recipient(raw: str | None) -> str:
    """The recipient number in E.164 form, or a ConfigError that names the setting."""
    if raw is None or not raw.strip():
        raise ConfigError(
            f"set {RECIPIENT_ENV} to your phone number in international format "
            "(for example +14155550123)"
        )
    number = raw.strip()
    if not _E164.fullmatch(number):  # fullmatch: "$" would accept a trailing newline
        raise ConfigError(
            f"{RECIPIENT_ENV} is not a valid international phone number "
            "(expected something like +14155550123)"
        )
    return number


def mask_number(number: str) -> str:
    """+14155550123 -> +***0123: only the last four digits are ever shown."""
    return f"+***{number[-4:]}"


def segment_count(body: str) -> int:
    """SMS segments for plain text: 160 characters, or 153 each once it is split."""
    return 1 if len(body) <= 160 else math.ceil(len(body) / 153)


def send_notification(
    message: Message | None, recipient: str, sender: SmsSender
) -> SendResult:
    """Send `message` once, unchanged. Raises SendError if it still fails.

    A None message means there is nothing to send: the sender is never called.
    """
    if message is None:
        return SendResult(
            accepted=False, message_id=None, attempts=0, reason="nothing_to_send"
        )

    masked = mask_number(recipient)
    segments = segment_count(message.body)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info(
            "sms_attempt attempt=%d to=%r chars=%d segments=%d",
            attempt,
            masked,
            len(message.body),
            segments,
        )
        try:
            message_id = sender(recipient, message.body)
        except SendError as error:
            error.attempts = attempt
            will_retry = error.retryable and attempt < MAX_ATTEMPTS
            logger.warning(
                "sms_failed attempt=%d reason=%r retry=%s",
                attempt,
                error.reason,
                "yes" if will_retry else "no",
            )
            if not will_retry:
                raise
            _sleep(BACKOFF_SECONDS)
        else:
            return SendResult(
                accepted=True, message_id=message_id, attempts=attempt, reason=None
            )
    raise AssertionError("unreachable")  # pragma: no cover
