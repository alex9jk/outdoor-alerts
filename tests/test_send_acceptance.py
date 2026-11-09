"""Acceptance tests for send_notification (the `send` command, AWS SNS email).

One end-to-end view through `gooddays.cli.main`, one clearly named test per
acceptance criterion of the approved story (AC1..AC15 in the test names), then the
edge cases. Nothing here can publish or touch the network: the sender is faked, or a
REAL boto3 client is used with a `before-send` hook that answers in place of AWS (so
no HTTP request ever leaves the process). Only obviously fake credentials are used.

Not testable here (documented, not covered): SNS accepts a publish for a topic whose
email subscription is unconfirmed and then delivers nothing. The code cannot see
that, so "sent" means "accepted by SNS" only (CLAUDE.md, "Sent means accepted").
"""

import dataclasses
import logging
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import (
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from conftest import REAL_NEW_CLIENT
from gooddays import cli, sns_sender
from gooddays.cli import main
from gooddays.models import Message, SendResult
from gooddays.notify import (
    BACKOFF_SECONDS,
    SUBJECT,
    TOPIC_ENV,
    SendError,
    send_notification,
)
from test_cli import SPOTS_YAML, FakeSender, write_saved_run, write_warm_dry_run

ARN = "arn:aws:sns:us-east-1:123456789012:good-days"
MASKED_ARN = "arn:aws:sns:us-east-1:********9012:good-days"
ACCOUNT_ID = "123456789012"
# What the saved warm, dry run turns into without Claude (--no-ai fallback text).
BODY = (
    "Today Mon Jun 15:\n"
    "Spot 0 hiking: 65 F, wind 5 mph, daylight 5:25-20:35\n"
    "Tomorrow Tue Jun 16:\n"
    "Spot 0 hiking: 65 F, wind 5 mph, daylight 5:25-20:36"
)
# Obviously fake credentials (never real), used only with the before-send hook.
FAKE_KEY = "AKIAFAKEFAKEFAKEFAKE"
FAKE_SECRET = "fake-secret-access-key-not-real-0000"
FAKE_TOKEN = "fake-session-token-not-real-1111"


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch working directory with a spots.yaml and a user agent set."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOODDAYS_NWS_USER_AGENT", "gooddays-test (t@example.com)")
    (tmp_path / "spots.yaml").write_text(SPOTS_YAML)
    return tmp_path


@pytest.fixture
def good_run(project: Path) -> Path:
    """A saved run with two good hiking days (a Message is produced with --no-ai)."""
    return write_warm_dry_run(project / "saved")


@pytest.fixture
def fake_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", FAKE_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", FAKE_SECRET)
    monkeypatch.setenv("AWS_SESSION_TOKEN", FAKE_TOKEN)


def send_args(folder: Path, *extra: str) -> list[str]:
    return ["send", "--no-ai", "--fixtures", str(folder), *extra]


def use_sender(monkeypatch: pytest.MonkeyPatch, sender: Any) -> None:
    monkeypatch.setattr(cli, "sns_sender", lambda: sender)


def forbid_sender(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Any attempt to build the sender fails the test; returns the (empty) build log."""
    built: list[int] = []

    def forbidden() -> Any:
        built.append(1)
        raise AssertionError("the notification sender must not be built here")

    monkeypatch.setattr(cli, "sns_sender", forbidden)
    return built


def use_message(monkeypatch: pytest.MonkeyPatch, message: Message | None) -> None:
    monkeypatch.setattr(cli, "generate_message", lambda *a, **k: message)
    # `send` builds the message from the facts that are new since the last send.
    monkeypatch.setattr(cli, "message_from_facts", lambda *a, **k: message)
    if message is None:
        real_build_facts = cli.build_facts

        def no_entries(*args: Any, **kwargs: Any) -> Any:
            return dataclasses.replace(real_build_facts(*args, **kwargs), entries=())

        monkeypatch.setattr(cli, "build_facts", no_entries)


# --- a real boto3 client answered by a before-send hook (no HTTP is ever sent) ---


class _Raw:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self) -> Iterator[bytes]:
        yield self._body


def _response(status: int, xml: str) -> AWSResponse:
    return AWSResponse(
        "https://sns.us-east-1.amazonaws.com/", status, {}, _Raw(xml.encode())
    )


def error_response(status: int, code: str) -> AWSResponse:
    return _response(
        status,
        '<ErrorResponse xmlns="http://sns.amazonaws.com/doc/2010-03-31/"><Error>'
        f"<Type>Receiver</Type><Code>{code}</Code><Message>x</Message></Error>"
        "<RequestId>r</RequestId></ErrorResponse>",
    )


def ok_response(message_id: str | None = "aws-id-0001") -> AWSResponse:
    tag = f"<MessageId>{message_id}</MessageId>" if message_id else ""
    return _response(
        200,
        '<PublishResponse xmlns="http://sns.amazonaws.com/doc/2010-03-31/">'
        f"<PublishResult>{tag}</PublishResult>"
        "<ResponseMetadata><RequestId>r</RequestId></ResponseMetadata>"
        "</PublishResponse>",
    )


class Aws:
    """Scripted AWS: each HTTP try pops the next outcome (a response or an exception)."""

    def __init__(self, *outcomes: AWSResponse | Exception) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[Any] = []  # one entry per HTTP try
        self.regions: list[str] = []  # one entry per client built

    def hook(self, **kwargs: Any) -> AWSResponse:
        self.requests.append(kwargs["request"])
        outcome = self.outcomes[min(len(self.requests), len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def new_client(self, region: str) -> Any:
        self.regions.append(region)
        client = REAL_NEW_CLIENT(region)
        client.meta.events.register("before-send.sns.Publish", self.hook)
        return client

    def form(self, index: int = 0) -> dict[str, list[str]]:
        body = self.requests[index].body
        text = body.decode() if isinstance(body, bytes) else str(body)
        return parse_qs(text)


def use_aws(monkeypatch: pytest.MonkeyPatch, *outcomes: AWSResponse | Exception) -> Aws:
    aws = Aws(*outcomes)
    monkeypatch.setattr(sns_sender, "_new_client", aws.new_client)
    return aws


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
    }


# --- AC1: sending requested, message present, topic and credentials available ---


def test_ac1_body_is_published_once_unchanged_with_the_fixed_subject(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    sender = FakeSender("msg-1")
    use_sender(monkeypatch, sender)

    assert main(send_args(good_run)) == 0

    out = capsys.readouterr().out
    assert sender.calls == [(ARN, SUBJECT, BODY)]  # once, unchanged, to the topic
    assert out.splitlines()[:-1] == BODY.splitlines()
    assert out.splitlines()[-1].startswith(f"sent to {MASKED_ARN} (accepted by SNS")
    assert "id=msg-1, attempts=1" in out


def test_ac1_real_client_request_carries_topic_subject_and_body(
    good_run: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_credentials: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    aws = use_aws(monkeypatch, ok_response("aws-id-42"))

    assert main(send_args(good_run)) == 0

    assert len(aws.requests) == 1
    form = aws.form()
    assert form["Action"] == ["Publish"]
    assert form["TopicArn"] == [ARN]
    assert form["Subject"] == [SUBJECT]
    assert form["Message"] == [BODY]
    assert "id=aws-id-42, attempts=1" in capsys.readouterr().out


# --- AC2: --dry-run needs no sender, topic or credentials (also under CI) ---


@pytest.mark.parametrize("ci", [None, "true"])
@pytest.mark.parametrize("topic", [None, "not-a-valid-arn"])
def test_ac2_dry_run_prints_the_text_and_needs_nothing(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    ci: str | None,
    topic: str | None,
) -> None:
    built = forbid_sender(monkeypatch)
    if ci is not None:
        monkeypatch.setenv("CI", ci)
    if topic is not None:
        monkeypatch.setenv(TOPIC_ENV, topic)  # not even looked at

    assert main(send_args(good_run, "--dry-run")) == 0

    out, err = capsys.readouterr()
    assert out.rstrip("\n") == BODY
    assert "dry run: not sent" in err
    assert built == []


# --- AC3: nothing to send ---


def test_ac3_nothing_to_send_makes_no_call_and_reads_no_settings(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_saved_run(project / "saved")  # sunny fixture: no good day
    built = forbid_sender(monkeypatch)
    # An invalid topic and CI set would each fail a real send with exit 2: they only
    # pass if the environment is not read at all.
    monkeypatch.setenv(TOPIC_ENV, "garbage")
    monkeypatch.setenv("CI", "true")

    assert main(send_args(folder)) == 0

    assert capsys.readouterr().out == "nothing to send\n"
    assert built == []


def test_ac3_a_none_message_never_calls_the_sender() -> None:
    sender = FakeSender("never")

    result = send_notification(None, ARN, sender)

    assert sender.calls == []
    assert result == SendResult(
        accepted=False, message_id=None, attempts=0, reason="nothing_to_send"
    )


# --- AC4: missing topic / no AWS credentials stop before publishing (exit 2) ---


@pytest.mark.parametrize("value", [None, "", "   "])
def test_ac4_missing_topic_exits_2_naming_the_setting(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    built = forbid_sender(monkeypatch)
    if value is not None:
        monkeypatch.setenv(TOPIC_ENV, value)

    assert main(send_args(good_run)) == 2

    out, err = capsys.readouterr()
    assert TOPIC_ENV in err
    assert "sent to" not in out
    assert built == []


def test_ac4_no_aws_credentials_exits_2_before_any_http_request(
    good_run: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The REAL boto3 client with an empty credential chain (conftest clears the AWS
    # settings and points the config files away). The hook must never be reached.
    monkeypatch.setenv(TOPIC_ENV, ARN)
    aws = use_aws(monkeypatch, ok_response())

    assert main(send_args(good_run)) == 2

    out, err = capsys.readouterr()
    assert aws.requests == []  # nothing was published
    assert "no AWS credentials found" in err and "AWS_PROFILE" in err
    assert "AWS_ACCESS_KEY_ID" in err
    assert "sent to" not in out and BODY not in out


# --- AC5: invalid topic ARN exits 2 before any provider call, value never echoed ---


@pytest.mark.parametrize(
    "bad",
    [
        "plain-topic-name-zzz",
        "arn:aws:sqs:us-east-1:123456789012:zzz-queue",  # not SNS
        "arn:aws:sns:us-east-1:123456789012:",  # no topic name
        "arn:aws:sns:us-east-1:98765432101:zzz-topic",  # 11-digit account
        "arn:aws:sns:us-east-1:98765432101234:zzz-topic",  # 14-digit account
        "arn:aws:sns:us-east-1:98765abc4321:zzz-topic",  # non-numeric account
        "arn:aws:sns::987654321012:zzz-topic",  # no region
        "arn:aws:sns:US-EAST-1:987654321012:zzz-topic",  # bad region case
        "arn:aws:sns:us-east-1:987654321012:zzz topic",  # space in the name
        "arn:aws:sns:us-east-1:987654321012:zzz-topic:extra",  # subscription ARN shape
        "https://sns.us-east-1.amazonaws.com/987654321012/zzz-topic",
    ],
)
def test_ac5_invalid_topic_exits_2_without_calling_or_echoing(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bad: str,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, bad)
    built = forbid_sender(monkeypatch)

    with caplog.at_level(logging.INFO):
        assert main(send_args(good_run)) == 2

    out, err = capsys.readouterr()
    assert TOPIC_ENV in err
    everything = out + err + caplog.text
    assert "zzz" not in everything and "987654321012" not in everything
    assert built == []


# --- AC6: credentials and the full account id never appear anywhere ---


@pytest.mark.parametrize(
    "outcome",
    [
        ok_response(),
        error_response(403, "AuthorizationError"),
        error_response(500, "InternalError"),
        ReadTimeoutError(endpoint_url="https://sns.us-east-1.amazonaws.com"),
    ],
    ids=["success", "auth_rejected", "server_error", "read_timeout"],
)
def test_ac6_credentials_and_full_account_id_never_appear(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_credentials: None,
    outcome: AWSResponse | Exception,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_aws(monkeypatch, outcome)  # the real client signs with the fake credentials

    # The CLI itself logs at INFO (basicConfig); that is the level checked here.
    with caplog.at_level(logging.INFO):
        main(send_args(good_run))

    out, err = capsys.readouterr()
    everything = out + err + caplog.text
    for secret in (FAKE_KEY, FAKE_SECRET, FAKE_TOKEN, ACCOUNT_ID):
        assert secret not in everything
    assert MASKED_ARN in everything  # the account id is shown masked, ********NNNN


# --- AC7: the body is not logged ---


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO])
def test_ac7_message_body_is_not_logged(
    good_run: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    level: int,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender("msg-1"))

    with caplog.at_level(level):
        assert main(send_args(good_run)) == 0

    assert "notify_attempt" in caplog.text  # logging is active, just without the text
    assert BODY not in caplog.text
    for line in BODY.splitlines():
        assert line not in caplog.text


# --- AC8: retry rules, exactly one HTTP try per attempt (real client + hook) ---


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (EndpointConnectionError(endpoint_url="https://sns"), "connection_error"),
        (ConnectTimeoutError(endpoint_url="https://sns"), "connect_timeout"),
        (error_response(500, "InternalError"), "unavailable"),
        (error_response(503, "ServiceUnavailable"), "unavailable"),
    ],
    ids=["connection", "connect_timeout", "http_500", "http_503"],
)
def test_ac8_retryable_failures_make_exactly_two_tries_with_a_wait(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_credentials: None,
    no_notify_sleep: list[float],
    outcome: AWSResponse | Exception,
    reason: str,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    aws = use_aws(monkeypatch, outcome)

    assert main(send_args(good_run)) == 4

    assert len(aws.requests) == 2  # 2 attempts in total, botocore added none
    assert no_notify_sleep == [BACKOFF_SECONDS]  # one wait between them
    assert f"reason={reason}, attempts=2" in capsys.readouterr().err


def test_ac8_a_retry_that_succeeds_exits_0_after_two_tries(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_credentials: None,
    no_notify_sleep: list[float],
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    aws = use_aws(monkeypatch, error_response(503, "ServiceUnavailable"), ok_response())

    assert main(send_args(good_run)) == 0

    assert len(aws.requests) == 2 and no_notify_sleep == [BACKOFF_SECONDS]
    assert "attempts=2" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (ReadTimeoutError(endpoint_url="https://sns"), "read_timeout"),
        (error_response(403, "AuthorizationError"), "auth_rejected"),
        (error_response(404, "NotFound"), "topic_not_found"),
        (error_response(429, "Throttled"), "throttled"),
        (error_response(400, "InvalidParameter"), "rejected"),
    ],
    ids=["read_timeout", "auth_403", "not_found_404", "throttled_429", "invalid_400"],
)
def test_ac8_non_retryable_failures_make_exactly_one_try(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_credentials: None,
    no_notify_sleep: list[float],
    outcome: AWSResponse | Exception,
    reason: str,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    aws = use_aws(monkeypatch, outcome)

    assert main(send_args(good_run)) == 4

    assert len(aws.requests) == 1  # a text is not idempotent: never retried
    assert no_notify_sleep == []
    assert f"reason={reason}, attempts=1" in capsys.readouterr().err


# --- AC9: failure output ---


@pytest.mark.parametrize(
    "error",
    [
        SendError("unavailable", retryable=True),
        SendError("auth_rejected"),
        SendError("throttled"),
    ],
    ids=lambda e: e.reason,
)
def test_ac9_failure_exits_4_prints_the_text_and_a_reason_key(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: SendError,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    sender = FakeSender(error)
    use_sender(monkeypatch, sender)

    assert main(send_args(good_run)) == 4

    out, err = capsys.readouterr()
    assert out.rstrip("\n") == BODY  # the text is not lost, and nothing claims success
    attempts = len(sender.calls)
    assert f"reason={error.reason}, attempts={attempts}" in err
    assert "may still have been delivered" not in err


def test_ac9_after_a_read_timeout_the_output_says_it_may_have_been_delivered(
    good_run: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender(SendError("read_timeout")))

    assert main(send_args(good_run)) == 4

    out, err = capsys.readouterr()
    assert out.rstrip("\n") == BODY
    assert "reason=read_timeout, attempts=1" in err
    assert "may still have been delivered" in err


# --- AC10: the outcome record, and nothing is stored ---


def test_ac10_send_result_is_frozen_and_carries_the_four_fields() -> None:
    assert [f.name for f in dataclasses.fields(SendResult)] == [
        "accepted",
        "message_id",
        "attempts",
        "reason",
    ]
    result = send_notification(
        Message(body="x", source="fallback", covered=()), ARN, FakeSender("id-9")
    )
    assert result == SendResult(
        accepted=True, message_id="id-9", attempts=1, reason=None
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.accepted = False  # type: ignore[misc]


@pytest.mark.parametrize("outcome", ["msg-1", SendError("auth_rejected")])
def test_ac10_send_writes_only_its_sent_record(
    project: Path,
    good_run: Path,
    sent_record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str | SendError,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender(outcome))
    before = _tree(project)

    main(send_args(good_run))

    after = _tree(project)
    changed = {
        Path(path)
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    }
    state = sent_record_path.parent
    assert all(path == state or state in path.parents for path in changed)
    assert not (project / "data").exists()


# --- AC11: the repeat warning ---


def test_ac11_a_real_send_warns_about_the_audience_not_about_repeats(
    good_run: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    use_sender(monkeypatch, FakeSender("msg-1"))

    assert main(send_args(good_run)) == 0

    out, err = capsys.readouterr()
    assert "warning:" in err and "every confirmed subscriber" in err
    assert "sends the same text again" not in err  # repeats are now skipped
    assert "warning" not in out


def test_ac11_a_dry_run_does_not_warn(
    good_run: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_sender(monkeypatch)

    assert main(send_args(good_run, "--dry-run")) == 0

    err = capsys.readouterr().err
    assert "sends the same text again" not in err and "warning:" not in err


# --- AC12: every spot failed ---


def test_ac12_every_spot_failed_exits_1_with_nothing_published(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "empty").mkdir()
    monkeypatch.setenv(TOPIC_ENV, ARN)
    built = forbid_sender(monkeypatch)

    assert main(send_args(project / "empty")) == 1

    assert capsys.readouterr().out == "nothing to send\n"
    assert built == []


# --- AC13: the other commands are unchanged and never load boto3 ---


def test_ac13_message_never_builds_a_sender_even_with_a_topic(
    good_run: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    built = forbid_sender(monkeypatch)

    assert main(["message", "--no-ai", "--fixtures", str(good_run)]) == 0

    assert capsys.readouterr().out.rstrip("\n") == BODY
    assert built == []


@pytest.mark.parametrize("command", ["fetch", "score", "message"])
def test_ac13_other_commands_do_not_import_boto3_or_the_sender(
    good_run: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    extra = ["--no-ai"] if command == "message" else []
    code = (
        "import sys\n"
        "from gooddays.cli import main\n"
        f"code = main([{command!r}, *{extra!r}, '--fixtures', {str(good_run)!r}])\n"
        "print('CODE', code, 'boto3' in sys.modules, "
        "'gooddays.sns_sender' in sys.modules)\n"
    )

    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert "CODE 0 False False" in done.stdout, done.stderr


# --- AC14: tests cannot publish or touch the network ---


def test_ac14_forgetting_to_fake_the_sender_ends_in_exit_70(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_credentials: None,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)

    with caplog.at_level(logging.ERROR):
        assert main(send_args(good_run)) == 70  # the autouse guard stopped it

    assert "fake the sender" in caplog.text
    assert "unexpected failure" in capsys.readouterr().err


# --- AC15: the CI guard ---


@pytest.mark.parametrize("value", ["true", "1", "false", "0", "no", " "])
def test_ac15_ci_set_to_any_non_empty_value_refuses_a_real_send(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    monkeypatch.setenv("CI", value)
    built = forbid_sender(monkeypatch)

    assert main(send_args(good_run)) == 2

    out, err = capsys.readouterr()
    assert "refusing to send a real email when CI is set" in err
    assert "sent to" not in out
    assert built == []


# --- edge cases ---


def test_edge_a_306_character_body_is_published_as_is(
    good_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = ("Today Mon Jun 15: " + "a" * 288)[:306]
    assert len(body) == 306
    use_message(monkeypatch, Message(body=body, source="ai", covered=()))
    monkeypatch.setenv(TOPIC_ENV, ARN)
    sender = FakeSender("msg-1")
    use_sender(monkeypatch, sender)

    assert main(send_args(good_run)) == 0

    assert sender.calls == [(ARN, SUBJECT, body)]  # one publish, no split or cut


@pytest.mark.parametrize("source", ["ai", "fallback"])
def test_edge_ai_and_fallback_messages_are_sent_the_same_way(
    good_run: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    use_message(monkeypatch, Message(body=BODY, source=source, covered=()))  # type: ignore[arg-type]
    monkeypatch.setenv(TOPIC_ENV, ARN)
    sender = FakeSender("msg-1")
    use_sender(monkeypatch, sender)

    assert main(send_args(good_run)) == 0

    assert sender.calls == [(ARN, SUBJECT, BODY)]


def test_edge_a_china_partition_topic_uses_its_own_region(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_credentials: None,
) -> None:
    arn = "arn:aws-cn:sns:cn-north-1:123456789012:good-days"
    monkeypatch.setenv(TOPIC_ENV, arn)
    aws = use_aws(monkeypatch, ok_response())

    assert main(send_args(good_run)) == 0

    assert aws.regions == ["cn-north-1"]
    assert "cn-north-1" in aws.requests[0].url
    assert aws.form()["TopicArn"] == [arn]
    out = capsys.readouterr().out
    assert "arn:aws-cn:sns:cn-north-1:********9012:good-days" in out
    assert ACCOUNT_ID not in out


def test_edge_a_different_region_is_taken_from_the_arn(
    good_run: Path, monkeypatch: pytest.MonkeyPatch, fake_credentials: None
) -> None:
    arn = "arn:aws:sns:eu-west-2:123456789012:good-days"
    monkeypatch.setenv(TOPIC_ENV, arn)
    aws = use_aws(monkeypatch, ok_response())

    assert main(send_args(good_run)) == 0

    assert aws.regions == ["eu-west-2"] and "eu-west-2" in aws.requests[0].url


def test_edge_whitespace_around_the_topic_value_is_ignored(
    good_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOPIC_ENV, f"  {ARN}\n")
    sender = FakeSender("msg-1")
    use_sender(monkeypatch, sender)

    assert main(send_args(good_run)) == 0

    assert sender.calls == [(ARN, SUBJECT, BODY)]


def test_edge_a_publish_without_a_message_id_is_not_accepted(
    good_run: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_credentials: None,
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)
    aws = use_aws(monkeypatch, ok_response(message_id=None))

    assert main(send_args(good_run)) == 4

    out, err = capsys.readouterr()
    assert len(aws.requests) == 1  # a bad response is not retried
    assert "reason=bad_response, attempts=1" in err
    assert out.rstrip("\n") == BODY and "sent to" not in out


def test_edge_ctrl_c_during_the_send_exits_130(
    good_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)

    def interrupt(destination: str, subject: str, body: str) -> str:
        raise KeyboardInterrupt

    use_sender(monkeypatch, interrupt)

    assert main(send_args(good_run)) == 130


def test_edge_an_unexpected_sender_exception_exits_70(
    good_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOPIC_ENV, ARN)

    def explode(destination: str, subject: str, body: str) -> str:
        raise RuntimeError("boom")

    use_sender(monkeypatch, explode)

    assert main(send_args(good_run)) == 70


def test_edge_empty_ci_value_is_not_a_ci_environment(
    good_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The story says "any non-empty value": an empty CI does not block a real send.
    monkeypatch.setenv(TOPIC_ENV, ARN)
    monkeypatch.setenv("CI", "")
    use_sender(monkeypatch, FakeSender("msg-1"))

    assert main(send_args(good_run)) == 0
