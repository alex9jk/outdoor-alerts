"""Acceptance tests for dedup of `send` (criteria 1-12 of the confirmed list).

Complements tests/test_sent.py (the builder's tests) with only the gaps: the
good -> bad -> good case, no footer / no Claude call when nothing is new, what the
record file contains, the default path, --resend with --dry-run, the lock and an
unwritable record through the command, and pruning through the command.
Nothing here can publish or touch the network or the real data/sent.json.
"""

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path

import pytest

from gooddays import cli
from gooddays.cli import main
from gooddays.models import EntryKey
from gooddays.notify import TOPIC_ENV
from gooddays.sent import LocalSentStore
from test_cli import SPOTS_YAML, FakeSender, write_saved_run, write_warm_dry_run
from test_send_acceptance import ARN, BODY, forbid_sender, send_args, use_sender

NOW = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
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


def forbid_writer(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Building the writer is lazy (no call); only INVOKING it is recorded and fails."""
    calls: list[int] = []

    def write(facts: str, max_chars: int) -> str:
        calls.append(1)
        raise AssertionError("Claude must not be called here")

    monkeypatch.setattr(cli, "anthropic_writer", lambda: write)
    return calls


def seed(path: Path, *keys: EntryKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    LocalSentStore(path).record(list(keys), NOW, "msg-0", NOW.date())


# --- 1: good -> bad -> good does not notify again ---


def test_c1_good_then_bad_then_good_again_is_not_sent_twice(
    project: Path, good_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    sender = FakeSender("msg-1")
    use_sender(monkeypatch, sender)
    assert main(send_args(good_run)) == 0  # good: sent

    bad = write_saved_run(project / "bad")  # same dates, no good day
    assert main(send_args(bad)) == 0  # bad: nothing

    assert main(send_args(good_run)) == 0  # good again

    assert len(sender.calls) == 1


# --- 2: nothing new -> nothing to send, no Claude, no SNS, no footer ---


def test_c2_nothing_new_has_no_claude_call_no_sns_and_no_footer(
    project: Path,
    good_run: Path,
    sent_record_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (good_run / "spot-1" / "open_meteo.json").unlink()  # spot-1 now has a fetch error
    seed(sent_record_path, KEY_TODAY, KEY_TOMORROW)
    built = forbid_sender(monkeypatch)
    writer_built = forbid_writer(monkeypatch)
    monkeypatch.setenv(TOPIC_ENV, "garbage")  # must not even be read

    # No --no-ai on purpose: only "nothing new" keeps Claude out of it.
    assert main(["send", "--fixtures", str(good_run)]) == 0

    out = capsys.readouterr().out
    assert out == "nothing to send\n"
    assert "couldn't be checked" not in out
    assert built == [] and writer_built == []


# --- 3 (extra): a failed send with an existing record leaves it unchanged ---


def test_c3_failed_send_leaves_an_existing_record_unchanged(
    good_run: Path, sent_record_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gooddays.notify import SendError

    seed(sent_record_path, KEY_TODAY)
    before = sent_record_path.read_text()
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender(SendError("read_timeout")))

    assert main(send_args(good_run)) == 4

    assert sent_record_path.read_text() == before


# --- 5: unwritable record is checked before sending ---


def test_c5_unwritable_state_exits_2_naming_the_setting_and_sends_nothing(
    good_run: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    built = forbid_sender(monkeypatch)
    monkeypatch.setenv(TOPIC_ENV, ARN)

    assert main(send_args(good_run, "--state", str(blocker / "sent.json"))) == 2

    err = capsys.readouterr().err
    assert "--state" in err and "Traceback" not in err
    assert built == []


# --- 6: retention through the command ---


def test_c6_a_successful_send_prunes_records_older_than_7_days(
    good_run: Path, sent_record_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent_record_path.parent.mkdir(parents=True)
    sent_record_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"spot_id": "old", "activity": "hiking", "date": "2020-01-01"}
                ],
            }
        )
    )
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender("msg-1"))

    assert main(send_args(good_run)) == 0

    assert LocalSentStore(sent_record_path).sent_keys() == {KEY_TODAY, KEY_TOMORROW}


# --- 8: --resend with --dry-run ---


def test_c8_resend_with_dry_run_prints_everything_and_writes_nothing(
    good_run: Path,
    sent_record_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(sent_record_path, KEY_TODAY, KEY_TOMORROW)
    before = sent_record_path.read_text()
    built = forbid_sender(monkeypatch)

    assert main(send_args(good_run, "--dry-run", "--resend")) == 0

    out, err = capsys.readouterr()
    assert out.rstrip("\n") == BODY
    assert "skipped" not in err
    assert sent_record_path.read_text() == before
    assert built == []


# --- 9: default path and what the record contains ---


def test_c9_default_state_path_is_data_sent_json() -> None:
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gooddays import cli; print(cli.DEFAULT_SENT_PATH)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.stdout.strip() == str(Path("data/sent.json")), done.stderr


def test_c9_the_record_holds_no_topic_email_credentials_or_text(
    good_run: Path,
    sent_record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKEFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-not-real")
    use_sender(monkeypatch, FakeSender("msg-1"))

    assert main(send_args(good_run)) == 0

    text = sent_record_path.read_text()
    for forbidden in ("arn:", "123456789012", "good-days", "AKIA", "fake-secret", "@"):
        assert forbidden not in text
    for line in BODY.splitlines():
        assert line not in text
    data = json.loads(text)
    assert set(data) == {"version", "entries"}
    for row in data["entries"]:
        assert set(row) == {"spot_id", "activity", "date", "sent_at", "message_id"}
        assert row["message_id"] == "msg-1"


# --- 10: the lock through the command ---


def test_c10_a_second_run_waits_then_exits_2_and_sends_nothing(
    good_run: Path,
    sent_record_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "LocalSentStore", partial(LocalSentStore, lock_wait=0.2))
    built = forbid_sender(monkeypatch)
    monkeypatch.setenv(TOPIC_ENV, ARN)

    with LocalSentStore(sent_record_path).lock():  # "another run" holds it
        assert main(send_args(good_run)) == 2

    err = capsys.readouterr().err
    assert "another run is using" in err and "nothing was sent" in err
    assert built == []
    assert not sent_record_path.exists()


def test_c10_the_lock_is_held_while_publishing(
    good_run: Path, sent_record_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gooddays.storage import ConfigError

    held: list[bool] = []

    def sender(destination: str, subject: str, body: str) -> str:
        other = LocalSentStore(sent_record_path, lock_wait=0.1)
        try:
            with other.lock():
                held.append(False)
        except ConfigError:
            held.append(True)
        return "msg-1"

    use_sender(monkeypatch, sender)
    monkeypatch.setenv(TOPIC_ENV, ARN)

    assert main(send_args(good_run)) == 0

    assert held == [True]
