"""One handler per pipeline stage, for AWS Lambda behind Step Functions.

fetch -> score -> message -> send. Each stage reads what the one before wrote to the
object store under `runs/<execution_id>/`, writes its own result there, and returns
only keys and small flags (a Step Functions payload is capped at 256 KB).

Handlers raise on anything wrong, so the state machine marks the run failed. The
exception class NAMES are what the state machine matches on, so they are stable.
`send` has no retry of its own beyond `send_notification`'s: publishing is not
idempotent. Importing this module loads neither boto3 nor the Claude SDK.
"""

import asyncio
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gooddays.fetch.service import Fetchers, fetch_conditions
from gooddays.message import MessageWriter, build_facts, message_from_facts
from gooddays.models import EntryKey
from gooddays.notify import (
    AWS_SETUP_PROBLEMS,
    TOPIC_ENV,
    NotificationSender,
    SendError,
    mask_topic,
    parse_topic_arn,
    send_notification,
)
from gooddays.objectstore import ObjectStore
from gooddays.score import score_conditions
from gooddays.sent import LocalSentStore, SentStore, split_sent
from gooddays.serialize import (
    MessageStage,
    deserialize_fetch,
    deserialize_message_stage,
    deserialize_score,
    serialize_fetch,
    serialize_message_stage,
    serialize_score,
)
from gooddays.storage import ConfigError, LocalStorage, Storage

logger = logging.getLogger(__name__)

BUCKET_ENV = "GOODDAYS_RUN_BUCKET"
SPOTS_PATH_ENV = "GOODDAYS_SPOTS_PATH"
SENT_PATH_ENV = "GOODDAYS_SENT_PATH"


class StageError(Exception):
    """Base for the errors a stage raises on purpose."""


class InvalidEventError(StageError):
    """The event is missing a field, or a field is not what this stage expects."""


class AllSpotsFailedError(StageError):
    """Every spot failed to fetch (the CLI's exit 1)."""


class SendFailedError(StageError):
    """SNS did not accept the publish. Nothing was sent."""

    def __init__(self, reason: str, attempts: int) -> None:
        super().__init__(f"email not sent (reason={reason}, attempts={attempts})")
        self.reason = reason
        self.attempts = attempts


class SendOutcomeUnknownError(StageError):
    """A read timeout: the email may or may not have been published."""

    def __init__(self, attempts: int) -> None:
        super().__init__(
            f"email may still have been delivered (reason=read_timeout, "
            f"attempts={attempts})"
        )
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class Dependencies:
    """What a handler uses. Anything left out is built from the environment, lazily.

    Tests pass fakes for all of it. `writer=None` means "use Claude", not "no AI".
    """

    store: ObjectStore | None = None
    storage: Storage | None = None
    fetchers: Fetchers | None = None
    writer: MessageWriter | None = None
    sender: NotificationSender | None = None
    sent_store: SentStore | None = None
    clock: Callable[[], datetime] | None = None


# --- keys and events ---


# Step Functions execution names are at most 80 characters.
MAX_EXECUTION_ID_LENGTH = 80


def _execution_id(event: Mapping[str, Any]) -> str:
    value = event.get("execution_id")
    if (
        not isinstance(value, str)
        or not value
        or "/" in value
        or "\\" in value
        or value == "."
        or ".." in value
        or len(value) > MAX_EXECUTION_ID_LENGTH
        or not value.isprintable()  # also catches DEL and Unicode control characters
    ):
        raise InvalidEventError(
            f"event needs an execution_id of at most {MAX_EXECUTION_ID_LENGTH} "
            "printable characters, without '/', '\\' or '..'"
        )
    return value


def _key(execution_id: str, name: str) -> str:
    return f"runs/{execution_id}/{name}.json"


def _checked_key(event: Mapping[str, Any], execution_id: str, name: str) -> str:
    """The key for a stage file. One the event carries must be the derived one."""
    key = _key(execution_id, name)
    given = event.get(f"{name}_key")
    if given is not None and given != key:
        raise InvalidEventError(f"{name}_key does not belong to this execution")
    return key


def _result(event: Mapping[str, Any], **new: Any) -> dict[str, Any]:
    """The event to return: the earlier keys and flags, plus this stage's."""
    carried = {
        name: event[name]
        for name in (
            "execution_id",
            "fetch_key",
            "score_key",
            "message_key",
            "spots_ok",
            "spots_failed",
            "nothing_to_send",
        )
        if name in event
    }
    return {**carried, **new}


# --- default dependencies (built only when a stage needs them) ---


def _store(deps: Dependencies) -> ObjectStore:
    if deps.store is not None:
        return deps.store
    from gooddays.s3_store import S3ObjectStore  # loads boto3

    bucket = os.environ.get(BUCKET_ENV)
    if not bucket:
        raise ConfigError(f"{BUCKET_ENV} is not set")
    return S3ObjectStore(bucket)


def _storage(deps: Dependencies) -> Storage:
    return deps.storage or LocalStorage(
        Path(os.environ.get(SPOTS_PATH_ENV) or "spots.yaml")
    )


def _sent_store(deps: Dependencies) -> SentStore:
    return deps.sent_store or LocalSentStore(
        Path(os.environ.get(SENT_PATH_ENV) or "data/sent.json")
    )


def _writer(deps: Dependencies) -> MessageWriter:
    if deps.writer is not None:
        return deps.writer
    from gooddays.ai_writer import anthropic_writer  # loads the SDK

    return anthropic_writer()


def _sender(deps: Dependencies) -> NotificationSender:
    if deps.sender is not None:
        return deps.sender
    from gooddays.sns_sender import sns_email_sender  # loads boto3

    return sns_email_sender()


def _now(deps: Dependencies) -> datetime:
    return deps.clock() if deps.clock is not None else datetime.now(UTC)


# --- the four stages ---


def fetch(
    event: Mapping[str, Any], context: object = None, deps: Dependencies | None = None
) -> dict[str, Any]:
    deps = deps or Dependencies()
    execution_id = _execution_id(event)
    fetch_key = _checked_key(event, execution_id, "fetch")
    store = _store(deps)
    storage = _storage(deps)
    settings = storage.get_settings()
    spots = storage.get_spots()

    result = asyncio.run(
        fetch_conditions(
            spots,
            settings,
            fetchers=deps.fetchers,
            fetched_at=_now(deps) if deps.clock is not None else None,
        )
    )
    # Written first, so the per-spot errors are there even when the stage then fails.
    store.put_json(fetch_key, serialize_fetch(result))
    ok, failed = len(result.results), len(result.errors)
    logger.info("stage_done stage=fetch spots_ok=%d spot_errors=%d", ok, failed)
    if result.errors and not result.results:
        raise AllSpotsFailedError(f"every spot failed ({failed} errors)")
    return _result(
        event,
        execution_id=execution_id,
        fetch_key=fetch_key,
        spots_ok=ok,
        spots_failed=failed,
    )


def score(
    event: Mapping[str, Any], context: object = None, deps: Dependencies | None = None
) -> dict[str, Any]:
    deps = deps or Dependencies()
    execution_id = _execution_id(event)
    fetch_key = _checked_key(event, execution_id, "fetch")
    score_key = _checked_key(event, execution_id, "score")
    store = _store(deps)
    storage = _storage(deps)
    thresholds = storage.get_thresholds()  # ConfigError if invalid
    settings = storage.get_settings()

    fetched = deserialize_fetch(store.get_json(fetch_key))
    scores = score_conditions(fetched.results, thresholds, settings.timezone)
    store.put_json(score_key, serialize_score(scores))
    logger.info("stage_done stage=score spots=%d", len(scores.results))
    return _result(
        event, execution_id=execution_id, fetch_key=fetch_key, score_key=score_key
    )


def message(
    event: Mapping[str, Any], context: object = None, deps: Dependencies | None = None
) -> dict[str, Any]:
    deps = deps or Dependencies()
    execution_id = _execution_id(event)
    fetch_key = _checked_key(event, execution_id, "fetch")
    score_key = _checked_key(event, execution_id, "score")
    message_key = _checked_key(event, execution_id, "message")
    store = _store(deps)
    storage = _storage(deps)
    settings = storage.get_settings()
    spots = storage.get_spots()

    fetched = deserialize_fetch(store.get_json(fetch_key))
    scores = deserialize_score(store.get_json(score_key))
    facts = build_facts(
        spots, scores, fetched.results, fetched.errors, settings.timezone
    )
    # Same filter as the CLI: only entries not emailed before.
    facts, skipped = split_sent(facts, _sent_store(deps).sent_keys())
    logger.info(
        "stage_done stage=message new=%d already_sent=%d",
        len(facts.entries),
        len(skipped),
    )

    if not facts.entries:  # no Claude call, nothing for Send to do
        store.put_json(message_key, serialize_message_stage(MessageStage(facts, None)))
        return _result(
            event,
            execution_id=execution_id,
            fetch_key=fetch_key,
            score_key=score_key,
            message_key=message_key,
            nothing_to_send=True,
        )

    _check_record_writable(deps)  # before Claude is called
    text = message_from_facts(facts, spots, _writer(deps))
    store.put_json(message_key, serialize_message_stage(MessageStage(facts, text)))
    return _result(
        event,
        execution_id=execution_id,
        fetch_key=fetch_key,
        score_key=score_key,
        message_key=message_key,
        nothing_to_send=False,
    )


def send(
    event: Mapping[str, Any], context: object = None, deps: Dependencies | None = None
) -> dict[str, Any]:
    deps = deps or Dependencies()
    execution_id = _execution_id(event)
    nothing = event.get("nothing_to_send")
    if not isinstance(nothing, bool):
        raise InvalidEventError("event needs nothing_to_send (true or false)")
    if nothing:
        # Reads no topic, credentials, store or message: nothing happens at all.
        logger.info("stage_done stage=send sent=false reason='nothing to send'")
        return _result(event, execution_id=execution_id, sent=False)

    message_key = _checked_key(event, execution_id, "message")
    staged = deserialize_message_stage(_store(deps).get_json(message_key))
    if staged.message is None:
        raise InvalidEventError("nothing_to_send is false but there is no message")
    text = staged.message

    topic = parse_topic_arn(os.environ.get(TOPIC_ENV))  # ConfigError names the setting
    _check_record_writable(deps)  # before anything is published
    masked = mask_topic(topic)
    try:
        sent = send_notification(text, topic, _sender(deps))
    except SendError as error:
        if error.reason in AWS_SETUP_PROBLEMS:
            raise ConfigError(
                f"{AWS_SETUP_PROBLEMS[error.reason]}; nothing was sent"
            ) from None
        logger.error("stage_failed stage=send reason=%r topic=%s", error.reason, masked)
        if error.reason == "read_timeout":
            raise SendOutcomeUnknownError(error.attempts) from None
        raise SendFailedError(error.reason, error.attempts) from None

    recorded = _record(deps, text.covered, sent.message_id or "")
    logger.info(
        "stage_done stage=send sent=true topic=%s attempts=%d recorded=%s",
        masked,
        sent.attempts,
        recorded,
    )
    return _result(
        event,
        execution_id=execution_id,
        sent=True,
        message_id=sent.message_id,
        attempts=sent.attempts,
        recorded=recorded,
    )


def _check_record_writable(deps: Dependencies) -> None:
    """Refuse, before Claude or SNS is used, if the sent record cannot be saved.

    Without this, every send would succeed and then end with `recorded: false`, and the
    next run would email the same days again.
    """
    try:
        _sent_store(deps).check_writable()
    except ConfigError as e:
        raise ConfigError(
            f"{e} (for the handlers the file is set with {SENT_PATH_ENV}); "
            "nothing was sent"
        ) from None


def _record(deps: Dependencies, covered: Sequence[EntryKey], message_id: str) -> bool:
    """Remember what was emailed. The email is out, so any failure here only warns.

    Everything is inside the `try`, settings and time zone included: raising now would
    fail the execution after SNS accepted the publish, and a retry would email again.
    """
    try:
        now = _now(deps)
        zone = ZoneInfo(_storage(deps).get_settings().timezone)
        sent_store = _sent_store(deps)
        with sent_store.lock():
            sent_store.record(covered, now, message_id, now.astimezone(zone).date())
    except (ConfigError, OSError, ValueError, KeyError) as e:
        logger.warning(
            "sent_record_failed error=%s (the next run may email the same days again)",
            type(e).__name__,
        )
        return False
    return True
