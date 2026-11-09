"""Remembering which good days were already emailed, so a repeat run stays quiet.

An entry is identified by spot id + activity + absolute local date (`EntryKey`), and
is notified at most once. Stored: those three, when it was sent and the SNS message
id. Never the topic, the email address, credentials or the text itself.
"""

import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from gooddays.models import Activity, EntryKey, GoodEntry, MessageFacts
from gooddays.storage import ConfigError

try:  # POSIX only; without it runs go unlocked (see LocalSentStore.lock)
    import fcntl
except ImportError:  # pragma: no cover - exercised by monkeypatching in tests
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Past days can never be good again, so a week of history is plenty.
RETENTION_DAYS = 7
# A slow run (fetch retries, Claude, SNS retries) can outlast this; the waiting run then
# stops with exit 2, which is safe: nothing is sent twice.
LOCK_WAIT_SECONDS = 150.0
_LOCK_POLL_SECONDS = 0.25
_VERSION = 1


class SentLockError(ConfigError):
    """Another run holds the record, or it cannot be locked (still exit 2)."""


class SentStore(Protocol):
    """What the pipeline needs from the record of sent entries.

    Separate from the read-only `Storage` protocol on purpose: this one is written
    to. A DynamoDB version later would make `record` one conditional write per key.
    """

    def lock(self) -> AbstractContextManager[None]:
        """Held from the check through the record, so two runs cannot both send."""
        ...

    def sent_keys(self) -> frozenset[EntryKey]:
        """Every recorded key. Raises ConfigError when the record is unusable."""
        ...

    def record(
        self,
        keys: Sequence[EntryKey],
        sent_at: datetime,
        message_id: str,
        today: date,
    ) -> None:
        """Remember these keys as sent and drop records older than the retention."""
        ...


class LocalSentStore:
    """A small JSON file: `{"version": 1, "entries": [{...}, ...]}`.

    Deleting the file forgets everything (the next run starts empty).
    """

    def __init__(self, path: Path, lock_wait: float = LOCK_WAIT_SECONDS) -> None:
        self.path = path
        self._lock_path = path.with_name(path.name + ".lock")
        self._lock_wait = lock_wait

    # --- reading ---

    def sent_keys(self) -> frozenset[EntryKey]:
        return frozenset(_key_of(row) for row in self._rows())

    def check_writable(self, *, create: bool = True) -> None:
        """Fail now (exit 2), not after an email went out that cannot be recorded.

        With `create=False` nothing is written or created (used by `--dry-run`): the
        nearest folder that exists must be writable.
        """
        self._rows()
        directory = self.path.parent
        if not create:
            existing = directory
            while not existing.exists() and existing != existing.parent:
                existing = existing.parent
            if not existing.is_dir() or not os.access(existing, os.W_OK | os.X_OK):
                raise ConfigError(
                    f"cannot write the sent record {self.path}: {existing} is not a "
                    f"writable folder; use --state to choose another file"
                )
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".probe-"):
                pass
        except OSError as e:
            raise ConfigError(
                f"cannot write the sent record in {directory}: {e.strerror or e}; "
                f"use --state to choose another file"
            ) from e

    def _rows(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return []  # first run: nothing sent yet (check_writable names a bad folder)
        except (OSError, UnicodeDecodeError) as e:
            raise self._corrupt(f"cannot be read ({type(e).__name__})") from e
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise self._corrupt("is not valid JSON") from e
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            raise self._corrupt(f"is not a version {_VERSION} sent record")
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise self._corrupt("has no 'entries' list")
        rows: list[dict[str, Any]] = []
        for index, row in enumerate(entries):
            try:
                _key_of(row)
            except (KeyError, TypeError, ValueError) as e:
                raise self._corrupt(f"has a bad entry at position {index}") from e
            rows.append(row)
        return rows

    def _corrupt(self, why: str) -> ConfigError:
        return ConfigError(
            f"the sent record {self.path} {why}; delete it (everything is then "
            f"treated as not yet sent) or restore it from a backup"
        )

    # --- writing ---

    def record(
        self,
        keys: Sequence[EntryKey],
        sent_at: datetime,
        message_id: str,
        today: date,
    ) -> None:
        cutoff = today - timedelta(days=RETENTION_DAYS)
        kept = [
            row for row in self._rows() if date.fromisoformat(row["date"]) >= cutoff
        ]
        known = {_key_of(row) for row in kept}
        for key in keys:
            if key in known:
                continue
            known.add(key)
            kept.append(
                {
                    "spot_id": key.spot_id,
                    "activity": key.activity,
                    "date": key.day.isoformat(),
                    "sent_at": sent_at.isoformat(),
                    "message_id": message_id,
                }
            )
        self._write({"version": _VERSION, "entries": kept})

    def _write(self, data: dict[str, Any]) -> None:
        """Write to a temp file in the same folder, then swap it in (all or nothing)."""
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=directory, prefix=".sent-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise

    # --- locking ---

    def lock(self) -> AbstractContextManager[None]:
        return self._lock()

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        if fcntl is None:
            logger.warning("sent_lock_unavailable reason='no fcntl on this platform'")
            yield
            return
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self._lock_path, "a")  # noqa: SIM115 - closed in finally
        except OSError as e:
            raise SentLockError(
                f"cannot create the lock file {self._lock_path}: {e.strerror or e}; "
                f"use --state to choose another file"
            ) from e
        try:
            deadline = time.monotonic() + self._lock_wait
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SentLockError(
                            f"another run is using {self.path} and did not finish "
                            f"within {self._lock_wait:.0f} s; nothing was sent"
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS)
                except OSError as e:  # e.g. a filesystem that cannot lock
                    raise SentLockError(
                        f"cannot lock {self._lock_path}: {e.strerror or e}; "
                        f"use --state to choose another file; nothing was sent"
                    ) from e
            yield  # closing the handle in `finally` releases the lock
        finally:
            handle.close()


def _key_of(row: Any) -> EntryKey:
    """Parse one stored row. Unknown extra fields are ignored."""
    return EntryKey(
        spot_id=_text(row["spot_id"]),
        activity=cast(Activity, _text(row["activity"])),
        day=date.fromisoformat(_text(row["date"])),
    )


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("expected a non-empty string")
    return value


def split_sent(
    facts: MessageFacts, sent: frozenset[EntryKey]
) -> tuple[MessageFacts, tuple[GoodEntry, ...]]:
    """The facts with only the entries not sent before, and the entries skipped."""
    new = tuple(entry for entry in facts.entries if entry.key not in sent)
    skipped = tuple(entry for entry in facts.entries if entry.key in sent)
    return replace(facts, entries=new), skipped


def describe_skipped(skipped: Sequence[GoodEntry]) -> str:
    """For the stderr note, e.g. 'Mendon Ponds Park hiking Mon Nov 9, ...'."""
    return ", ".join(
        f"{e.spot_name} {e.activity.replace('_', ' ')} {e.day:%a %b} {e.day.day}"
        for e in skipped
    )
