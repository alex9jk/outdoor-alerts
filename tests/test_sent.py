"""The sent record: which good days were already emailed, so a repeat run stays quiet."""

import errno
import json
import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from gooddays import cli, sent
from gooddays.cli import main
from gooddays.models import EntryKey, GoodEntry, MessageFacts
from gooddays.notify import TOPIC_ENV, SendError
from gooddays.sent import LocalSentStore, describe_skipped, split_sent
from gooddays.storage import ConfigError
from test_cli import SPOTS_YAML, FakeSender, write_warm_dry_run
from test_message import entry
from test_send_acceptance import ARN, BODY, forbid_sender, send_args, use_sender

NOW = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
TODAY = date(2026, 6, 15)
# The two good days in the saved warm, dry run.
KEY_TODAY = EntryKey("spot-0", "hiking", date(2026, 6, 15))
KEY_TOMORROW = EntryKey("spot-0", "hiking", date(2026, 6, 16))


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOODDAYS_NWS_USER_AGENT", "gooddays-test (t@example.com)")
    (tmp_path / "spots.yaml").write_text(SPOTS_YAML)
    return tmp_path


@pytest.fixture
def good_run(project: Path) -> Path:
    return write_warm_dry_run(project / "saved")


@pytest.fixture
def store(tmp_path: Path) -> LocalSentStore:
    return LocalSentStore(tmp_path / "rec" / "sent.json", lock_wait=0.3)


def rows(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["entries"]


# --- the store ---


def test_no_file_means_nothing_sent_yet(store: LocalSentStore) -> None:
    assert store.sent_keys() == frozenset()


def test_record_then_read_back_and_store_only_the_allowed_fields(
    store: LocalSentStore,
) -> None:
    store.record([KEY_TODAY], NOW, "msg-1", TODAY)

    assert store.sent_keys() == {KEY_TODAY}
    assert rows(store.path) == [
        {
            "spot_id": "spot-0",
            "activity": "hiking",
            "date": "2026-06-15",
            "sent_at": NOW.isoformat(),
            "message_id": "msg-1",
        }
    ]


def test_recording_the_same_key_twice_keeps_one_row(store: LocalSentStore) -> None:
    store.record([KEY_TODAY], NOW, "msg-1", TODAY)
    store.record([KEY_TODAY, KEY_TOMORROW], NOW, "msg-2", TODAY)

    assert len(rows(store.path)) == 2
    assert rows(store.path)[0]["message_id"] == "msg-1"  # the first send is kept


def test_old_records_are_pruned_at_the_retention_boundary(
    store: LocalSentStore,
) -> None:
    old = EntryKey("spot-0", "hiking", date(2026, 6, 12))
    edge = EntryKey("spot-0", "hiking", date(2026, 6, 13))
    store.record([old, edge], NOW, "msg-1", date(2026, 6, 12))

    store.record([KEY_TODAY], NOW, "msg-2", date(2026, 6, 20))  # cutoff June 13

    assert store.sent_keys() == {edge, KEY_TODAY}


def test_unknown_fields_future_dates_and_duplicates_are_tolerated(
    store: LocalSentStore,
) -> None:
    store.path.parent.mkdir(parents=True)
    row = {"spot_id": "a", "activity": "hiking", "date": "2030-01-01", "extra": 1}
    store.path.write_text(json.dumps({"version": 1, "entries": [row, row]}))

    assert store.sent_keys() == {EntryKey("a", "hiking", date(2030, 1, 1))}


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"entries": []}',
        '{"version": 2, "entries": []}',
        '{"version": 1, "entries": {}}',
        '{"version": 1, "entries": ["x"]}',
        '{"version": 1, "entries": [{"spot_id": "a", "activity": "hiking"}]}',
        '{"version": 1, "entries": [{"spot_id": 5, "activity": "h", "date": "2026-06-15"}]}',
        '{"version": 1, "entries": [{"spot_id": "a", "activity": "h", "date": "June"}]}',
        "",
    ],
)
def test_an_unusable_file_is_a_config_error_naming_the_file(
    store: LocalSentStore, content: str
) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content)

    with pytest.raises(ConfigError, match=r"sent\.json.*delete it"):
        store.sent_keys()


def test_a_file_that_is_not_utf8_is_a_config_error(store: LocalSentStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b"\xff\xfe\x00bad")

    with pytest.raises(ConfigError, match=r"sent\.json"):
        store.sent_keys()


def test_a_failed_write_leaves_the_old_file_intact_and_no_temp_file(
    store: LocalSentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.record([KEY_TODAY], NOW, "msg-1", TODAY)
    before = store.path.read_text()

    def boom(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.record([KEY_TOMORROW], NOW, "msg-2", TODAY)

    assert store.path.read_text() == before
    assert [p.name for p in store.path.parent.iterdir()] == ["sent.json"]


def test_an_unwritable_location_is_a_config_error(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")

    with pytest.raises(ConfigError, match="--state"):
        LocalSentStore(blocker / "sent.json").check_writable()


def test_a_second_run_waits_then_stops_when_the_lock_is_held(
    store: LocalSentStore,
) -> None:
    other = LocalSentStore(store.path, lock_wait=0.2)
    with store.lock():
        with pytest.raises(ConfigError, match="another run is using"):
            with other.lock():
                pytest.fail("the lock should not have been granted")

    with other.lock():  # free again once the first run is done
        pass


def test_without_fcntl_the_run_goes_ahead_with_a_warning(
    store: LocalSentStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(sent, "fcntl", None)

    with caplog.at_level(logging.WARNING), store.lock():
        pass

    assert "sent_lock_unavailable" in caplog.text


# --- pure helpers ---


def test_the_key_ignores_the_today_tomorrow_label() -> None:
    assert entry(day_label="today").key == entry(day_label="tomorrow").key


def test_split_sent_keeps_only_new_entries_in_order() -> None:
    a = entry(spot_id="a", spot_name="A")
    b = entry(spot_id="b", spot_name="B")
    facts = MessageFacts(entries=(a, b), unchecked_spots=2)

    new, skipped = split_sent(facts, frozenset({a.key}))

    assert new.entries == (b,)
    assert new.unchecked_spots == 2
    assert skipped == (a,)


def test_describe_skipped_names_spot_activity_and_date() -> None:
    e: GoodEntry = entry(
        spot_name="Mendon Ponds Park",
        activity="cross_country_skiing",
        day=date(2026, 11, 9),
    )

    assert describe_skipped([e, e]) == (
        "Mendon Ponds Park cross country skiing Mon Nov 9, "
        "Mendon Ponds Park cross country skiing Mon Nov 9"
    )


# --- through the command ---


def test_a_repeat_send_says_nothing_new_and_publishes_nothing(
    good_run: Path,
    sent_record_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    sender = FakeSender("msg-1")
    use_sender(monkeypatch, sender)
    assert main(send_args(good_run)) == 0
    assert len(sender.calls) == 1
    assert {r["message_id"] for r in rows(sent_record_path)} == {"msg-1"}
    capsys.readouterr()

    forbid_sender(monkeypatch)  # no topic, no credentials, no publish
    assert main(send_args(good_run)) == 0

    out, err = capsys.readouterr()
    assert out == "nothing to send\n"
    assert (
        "skipped 2 already sent: Spot 0 hiking Mon Jun 15, Spot 0 hiking Tue Jun 16"
        in err
    )
    assert len(sender.calls) == 1


def test_a_new_day_alone_is_sent_without_the_old_ones(
    good_run: Path,
    sent_record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_record_path.parent.mkdir(parents=True)
    sent_record_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"spot_id": "spot-0", "activity": "hiking", "date": "2026-06-15"}
                ],
            }
        )
    )
    monkeypatch.setenv(TOPIC_ENV, ARN)
    sender = FakeSender("msg-2")
    use_sender(monkeypatch, sender)

    assert main(send_args(good_run)) == 0

    (call,) = sender.calls
    assert "Jun 16" in call[2] and "Jun 15" not in call[2]
    assert KEY_TOMORROW in LocalSentStore(sent_record_path).sent_keys()


@pytest.mark.parametrize("reason", ["auth_rejected", "unavailable", "read_timeout"])
def test_a_failed_or_unknown_send_records_nothing(
    good_run: Path,
    sent_record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender(SendError(reason)))

    assert main(send_args(good_run)) == 4

    assert not sent_record_path.exists()


def test_a_record_failure_after_the_send_warns_loudly_and_still_exits_0(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cannot_record(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(LocalSentStore, "record", cannot_record)
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender("msg-1"))

    assert main(send_args(good_run)) == 0

    err = capsys.readouterr().err
    assert "WARNING: the email was sent but could not be recorded" in err
    assert "may email the same days again" in err


def test_a_closed_stdout_after_the_publish_still_records(
    good_run: Path,
    sent_record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def closed(text: str) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(cli, "_emit", closed)
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender("msg-1"))

    assert main(send_args(good_run)) == cli.EXIT_BROKEN_PIPE

    assert LocalSentStore(sent_record_path).sent_keys() == {KEY_TODAY, KEY_TOMORROW}


@pytest.mark.parametrize("extra", [[], ["--dry-run"]])
def test_a_corrupt_record_exits_2_naming_the_file_before_anything_is_sent(
    good_run: Path,
    sent_record_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
) -> None:
    sent_record_path.parent.mkdir(parents=True)
    sent_record_path.write_text("{oops")
    built = forbid_sender(monkeypatch)
    monkeypatch.setenv(TOPIC_ENV, ARN)

    assert main(send_args(good_run, *extra)) == 2

    err = capsys.readouterr().err
    assert str(sent_record_path) in err and "delete it" in err
    assert "Traceback" not in err
    assert built == []


def test_resend_includes_sent_days_and_records_them_again(
    good_run: Path, sent_record_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    sender = FakeSender("msg-1")
    use_sender(monkeypatch, sender)
    assert main(send_args(good_run)) == 0

    assert main(send_args(good_run, "--resend")) == 0

    assert len(sender.calls) == 2
    assert sender.calls[1][2] == BODY
    assert LocalSentStore(sent_record_path).sent_keys() == {KEY_TODAY, KEY_TOMORROW}


def test_a_dry_run_skips_sent_days_and_writes_nothing(
    good_run: Path,
    sent_record_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_sender(monkeypatch)
    assert main(send_args(good_run, "--dry-run")) == 0
    assert not sent_record_path.exists()  # a dry run never records
    assert not sent_record_path.parent.exists()
    capsys.readouterr()

    sent_record_path.parent.mkdir(parents=True)
    LocalSentStore(sent_record_path).record(
        [KEY_TODAY, KEY_TOMORROW], NOW, "msg-1", TODAY
    )
    before = sent_record_path.read_text()

    assert main(send_args(good_run, "--dry-run")) == 0

    assert capsys.readouterr().out == "nothing to send\n"
    assert sent_record_path.read_text() == before


def test_the_message_command_ignores_the_sent_record(
    good_run: Path,
    sent_record_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent_record_path.parent.mkdir(parents=True)
    LocalSentStore(sent_record_path).record(
        [KEY_TODAY, KEY_TOMORROW], NOW, "msg-1", TODAY
    )

    assert main(["message", "--no-ai", "--fixtures", str(good_run)]) == 0

    assert capsys.readouterr().out.strip() == BODY


def test_only_the_entries_the_text_names_are_recorded(
    good_run: Path,
    sent_record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gooddays.models import Message

    shortened = Message(
        body="Today: Spot 0 hiking. +1 more", source="fallback", covered=(KEY_TODAY,)
    )
    monkeypatch.setattr(cli, "message_from_facts", lambda *a, **k: shortened)
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender("msg-1"))

    assert main(send_args(good_run)) == 0

    assert LocalSentStore(sent_record_path).sent_keys() == {KEY_TODAY}


def test_a_spot_removed_from_the_config_is_harmless(
    good_run: Path, sent_record_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent_record_path.parent.mkdir(parents=True)
    LocalSentStore(sent_record_path).record(
        [EntryKey("gone-spot", "hiking", TODAY)], NOW, "msg-0", TODAY
    )
    monkeypatch.setenv(TOPIC_ENV, ARN)
    sender = FakeSender("msg-1")
    use_sender(monkeypatch, sender)

    assert main(send_args(good_run)) == 0

    assert len(sender.calls) == 1


def test_state_option_chooses_another_file(
    good_run: Path,
    tmp_path: Path,
    sent_record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path / "elsewhere.json"
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender("msg-1"))

    assert main(send_args(good_run, "--state", str(other))) == 0

    assert other.exists() and not sent_record_path.exists()


def test_a_dry_run_also_rejects_an_unwritable_state_location_and_creates_nothing(
    good_run: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    forbid_sender(monkeypatch)

    code = main(send_args(good_run, "--dry-run", "--state", str(blocker / "sent.json")))

    assert code == 2
    assert "--state" in capsys.readouterr().err


def test_a_dry_run_with_a_missing_but_creatable_folder_creates_nothing(
    good_run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_sender(monkeypatch)
    state = tmp_path / "new" / "deeper" / "sent.json"

    assert main(send_args(good_run, "--dry-run", "--state", str(state))) == 0

    assert not (tmp_path / "new").exists()


def test_other_lock_errors_exit_2_instead_of_crashing(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cannot_lock(*args: Any) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(sent.fcntl, "flock", cannot_lock)
    built = forbid_sender(monkeypatch)

    assert main(send_args(good_run)) == 2

    err = capsys.readouterr().err
    assert err.startswith("error: cannot lock") and "nothing was sent" in err
    assert "Traceback" not in err and built == []


def test_a_lock_timeout_is_not_called_a_config_error(
    good_run: Path,
    sent_record_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sent, "LOCK_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(
        cli,
        "LocalSentStore",
        lambda path: LocalSentStore(path, lock_wait=0.1),
    )
    forbid_sender(monkeypatch)
    with LocalSentStore(sent_record_path).lock():
        assert main(send_args(good_run)) == 2

    err = capsys.readouterr().err
    assert err.startswith("error: another run is using")
