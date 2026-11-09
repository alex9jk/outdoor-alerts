"""send_notification(): hand the message to a sender, with a narrow retry.

Pure apart from the injected `sender` (the only thing that talks to a provider)
and `_sleep`. A notification is not idempotent: a retry after the request may
have left the machine could notify twice, so only failures that clearly
happened before the provider took the request are retried.
"""

import logging
import re
import time
from collections.abc import Callable

from gooddays.models import Message, SendResult
from gooddays.storage import ConfigError

logger = logging.getLogger(__name__)

TOPIC_ENV = "GOODDAYS_SNS_TOPIC_ARN"
# SNS emails need a subject. Kept plain on purpose: ASCII, one line, short, starting
# with a letter (SNS limits email subjects to about 100 characters; verify the details).
SUBJECT = "Good days to get outside"
MAX_ATTEMPTS = 2  # in total, including the first
BACKOFF_SECONDS = 1.0

# Indirection so tests can skip the real wait.
_sleep = time.sleep

# arn:<partition>:sns:<region>:<12-digit account>:<topic name>. A region looks like
# us-east-1 or cn-north-1. A FIFO topic's name ends in ".fifo", which this rejects on
# purpose: SNS email subscriptions need a standard topic.
_TOPIC_ARN = re.compile(
    r"arn:(?:aws|aws-cn|aws-us-gov|aws-iso(?:-[a-z])?):sns:"
    r"([a-z]{2}(?:-[a-z]+)+-\d{1,2}):(\d{12}):[A-Za-z0-9_-]{1,256}"
)

# (destination, subject, body) -> the provider's message id. Raises SendError.
NotificationSender = Callable[[str, str, str], str]


class SendError(Exception):
    """A send failed. `reason` is a short key that is safe to log and print.

    `retryable` is True only when the request clearly did not reach the provider
    or the provider reported its own failure (5xx). Never put provider response
    text in here.
    """

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.attempts = 0  # tries made, set by send_notification


def parse_topic_arn(raw: str | None) -> str:
    """The SNS topic ARN, or a ConfigError that names the setting."""
    if raw is None or not raw.strip():
        raise ConfigError(
            f"set {TOPIC_ENV} to the ARN of your SNS topic "
            "(like arn:aws:sns:us-east-1:123456789012:good-days)"
        )
    arn = raw.strip()
    if not _TOPIC_ARN.fullmatch(arn):  # fullmatch: "$" would accept a trailing newline
        raise ConfigError(
            f"{TOPIC_ENV} is not a valid SNS topic ARN "
            "(expected like arn:aws:sns:us-east-1:123456789012:good-days, "
            "a standard topic, not a FIFO topic)"
        )
    return arn


def topic_region(arn: str) -> str:
    """The AWS region a (valid) topic ARN lives in."""
    match = _TOPIC_ARN.fullmatch(arn)
    if match is None:
        raise ValueError("not a topic ARN")
    return match.group(1)


def mask_topic(arn: str) -> str:
    """Hide all but the last four digits of the AWS account id, wherever it appears."""
    match = _TOPIC_ARN.fullmatch(arn)
    if match is None:
        return "***"
    account = match.group(2)
    return arn.replace(account, f"********{account[-4:]}")


def send_notification(
    message: Message | None, destination: str, sender: NotificationSender
) -> SendResult:
    """Send `message` once, unchanged. Raises SendError if it still fails.

    A None message means there is nothing to send: the sender is never called.
    """
    if message is None:
        return SendResult(
            accepted=False, message_id=None, attempts=0, reason="nothing_to_send"
        )

    masked = mask_topic(destination)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info(
            "notify_attempt attempt=%d to=%r chars=%d",
            attempt,
            masked,
            len(message.body),
        )
        try:
            message_id = sender(destination, SUBJECT, message.body)
        except SendError as error:
            error.attempts = attempt
            will_retry = error.retryable and attempt < MAX_ATTEMPTS
            logger.warning(
                "notify_failed attempt=%d reason=%r retry=%s",
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
