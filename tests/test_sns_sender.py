import logging
from collections.abc import Iterator
from typing import Any

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import (
    ConfigParseError,
    ConnectionClosedError,
    ConnectTimeoutError,
    CredentialRetrievalError,
    EndpointConnectionError,
    InvalidConfigError,
    InvalidRegionError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
    ProfileNotFound,
    ReadTimeoutError,
    SSLError,
    SSOTokenLoadError,
    UnauthorizedSSOTokenError,
)
from botocore.stub import Stubber

from conftest import REAL_NEW_CLIENT
from gooddays import sns_sender
from gooddays.models import Message
from gooddays.notify import SUBJECT, SendError, send_notification
from gooddays.sns_sender import _CONFIG, TIMEOUT_SECONDS, sns_email_sender

ARN = "arn:aws:sns:us-east-1:123456789012:good-days"
BODY = "Today Mon Jun 15:\nSpot 0 hiking: 65 F, wind 5 mph"


def stubbed(monkeypatch: pytest.MonkeyPatch, region: str = "us-east-1") -> Stubber:
    """The real adapter with a real boto3 client whose responses we script."""
    client = REAL_NEW_CLIENT(region)
    stubber = Stubber(client)
    stubber.activate()
    monkeypatch.setattr(sns_sender, "_new_client", lambda region: client)
    return stubber


class FakeClient:
    """A client whose publish raises a given exception (for connection-level errors)."""

    def __init__(self, error: Exception) -> None:
        self.error, self.calls = error, 0

    def publish(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.error


# --- the request ---


def test_publish_sends_the_topic_subject_and_unchanged_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stubber = stubbed(monkeypatch)
    # Stubber also checks the parameters against SNS's own API model.
    stubber.add_response(
        "publish",
        {"MessageId": "11111111-2222-3333-4444-555555555555"},
        {"TopicArn": ARN, "Subject": SUBJECT, "Message": BODY},
    )

    message_id = sns_email_sender()(ARN, SUBJECT, BODY)

    assert message_id == "11111111-2222-3333-4444-555555555555"
    stubber.assert_no_pending_responses()


def test_the_region_comes_from_the_arn_and_each_client_is_built_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []

    class Ok:
        def publish(self, **kwargs: Any) -> dict[str, str]:
            return {"MessageId": "m-1"}

    def fake_new_client(region: str) -> Ok:
        built.append(region)
        return Ok()

    monkeypatch.setattr(sns_sender, "_new_client", fake_new_client)
    send = sns_email_sender()
    assert built == []  # creating the sender builds nothing (and needs no credentials)

    send(ARN, SUBJECT, BODY)
    send(ARN, SUBJECT, BODY)
    send("arn:aws:sns:eu-west-2:123456789012:other", SUBJECT, BODY)

    assert built == ["us-east-1", "eu-west-2"]


def test_the_client_has_explicit_timeouts_and_botocores_own_retries_are_off() -> None:
    assert TIMEOUT_SECONDS == 10
    assert _CONFIG.connect_timeout == _CONFIG.read_timeout == 10
    assert (
        _CONFIG.retries["total_max_attempts"] == 1
    )  # botocore adds its own "mode" key
    client = REAL_NEW_CLIENT("us-east-1")
    assert client.meta.region_name == "us-east-1"
    assert client.meta.config.retries["total_max_attempts"] == 1


def test_without_the_opt_in_a_real_client_cannot_be_made() -> None:
    # The autouse guard: forgetting to fake the sender fails loudly, never publishes.
    with pytest.raises(AssertionError, match="fake the sender"):
        sns_email_sender()(ARN, SUBJECT, BODY)


# --- exactly one HTTP try per call (botocore must not retry by itself) ---


@pytest.fixture
def aws_dummy_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret-key")


class _Raw:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self) -> Iterator[bytes]:
        yield self._body


def _error_response(status: int, code: str) -> AWSResponse:
    body = (
        '<ErrorResponse xmlns="http://sns.amazonaws.com/doc/2010-03-31/"><Error>'
        f"<Type>Receiver</Type><Code>{code}</Code><Message>x</Message></Error>"
        "<RequestId>r</RequestId></ErrorResponse>"
    ).encode()
    return AWSResponse("https://sns.us-east-1.amazonaws.com/", status, {}, _Raw(body))


def _count_http_tries(client: Any, status: int, code: str) -> list[int]:
    tries: list[int] = []

    def fake_http(**kwargs: Any) -> AWSResponse:
        tries.append(1)
        return _error_response(status, code)

    client.meta.events.register("before-send.sns.Publish", fake_http)
    return tries


@pytest.mark.parametrize(
    ("status", "code"), [(500, "InternalError"), (503, "ServiceUnavailable")]
)
def test_one_http_try_per_call_even_when_the_server_fails(
    monkeypatch: pytest.MonkeyPatch, aws_dummy_credentials: None, status: int, code: str
) -> None:
    client = REAL_NEW_CLIENT("us-east-1")
    tries = _count_http_tries(client, status, code)
    monkeypatch.setattr(sns_sender, "_new_client", lambda region: client)

    with pytest.raises(SendError) as exc:
        sns_email_sender()(ARN, SUBJECT, BODY)

    assert len(tries) == 1  # botocore did not retry on its own
    assert exc.value.reason == "unavailable" and exc.value.retryable is True


def test_the_control_default_retries_would_have_made_several_tries(
    monkeypatch: pytest.MonkeyPatch, aws_dummy_credentials: None
) -> None:
    """Shows the one-try test above is meaningful: botocore's default does retry."""
    import time

    import boto3

    monkeypatch.setattr(time, "sleep", lambda seconds: None)  # skip botocore's backoff
    client = boto3.client("sns", region_name="us-east-1")  # default retry behaviour
    tries = _count_http_tries(client, 500, "InternalError")

    with pytest.raises(Exception):  # noqa: B017 - any botocore error will do
        client.publish(TopicArn=ARN, Subject=SUBJECT, Message=BODY)

    assert len(tries) > 1


def test_the_retry_loop_makes_two_tries_in_total_and_no_more(
    monkeypatch: pytest.MonkeyPatch, aws_dummy_credentials: None
) -> None:
    client = REAL_NEW_CLIENT("us-east-1")
    tries = _count_http_tries(client, 503, "ServiceUnavailable")
    monkeypatch.setattr(sns_sender, "_new_client", lambda region: client)

    with pytest.raises(SendError) as exc:
        send_notification(Message(body=BODY, source="ai"), ARN, sns_email_sender())

    assert len(tries) == 2 and exc.value.attempts == 2


# --- errors from AWS: reason key and whether a retry is safe ---


@pytest.mark.parametrize(
    ("code", "status", "reason", "retryable"),
    [
        ("InternalError", 500, "unavailable", True),
        ("ServiceUnavailable", 503, "unavailable", True),
        ("AuthorizationError", 403, "auth_rejected", False),
        ("AccessDenied", 403, "auth_rejected", False),
        ("InvalidClientTokenId", 403, "auth_rejected", False),
        ("SignatureDoesNotMatch", 403, "auth_rejected", False),
        ("ExpiredToken", 400, "auth_rejected", False),
        ("NotFound", 404, "topic_not_found", False),
        ("Throttled", 429, "throttled", False),
        ("ThrottlingException", 400, "throttled", False),
        ("InvalidParameter", 400, "rejected", False),
        ("InvalidParameterValue", 400, "rejected", False),
        ("KMSAccessDenied", 400, "rejected", False),
        ("SomethingNew", 418, "rejected", False),
    ],
)
def test_aws_errors_map_to_a_reason_and_a_retry_decision(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int,
    reason: str,
    retryable: bool,
) -> None:
    stubber = stubbed(monkeypatch)
    stubber.add_client_error(
        "publish", service_error_code=code, http_status_code=status
    )

    with pytest.raises(SendError) as exc:
        sns_email_sender()(ARN, SUBJECT, BODY)

    assert exc.value.reason == reason and exc.value.retryable is retryable


@pytest.mark.parametrize(
    ("error", "reason", "retryable"),
    [
        (ConnectTimeoutError(endpoint_url="https://sns"), "connect_timeout", True),
        (EndpointConnectionError(endpoint_url="https://sns"), "connection_error", True),
        (ConnectionClosedError(endpoint_url="https://sns"), "connection_error", False),
        (ReadTimeoutError(endpoint_url="https://sns"), "read_timeout", False),
        (
            SSLError(endpoint_url="https://sns", error="bad cert"),
            "request_failed",
            False,
        ),
        (NoCredentialsError(), "no_credentials", False),
        (
            PartialCredentialsError(provider="env", cred_var="AWS_SECRET_ACCESS_KEY"),
            "no_credentials",
            False,
        ),
        (
            CredentialRetrievalError(provider="imds", error_msg="x"),
            "no_credentials",
            False,
        ),
        (ProfileNotFound(profile="nope"), "bad_aws_config", False),
    ],
)
def test_connection_and_credential_errors_map_to_a_reason_and_a_retry_decision(
    monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str, retryable: bool
) -> None:
    client = FakeClient(error)
    monkeypatch.setattr(sns_sender, "_new_client", lambda region: client)

    with pytest.raises(SendError) as exc:
        sns_email_sender()(ARN, SUBJECT, BODY)

    assert exc.value.reason == reason and exc.value.retryable is retryable
    assert client.calls == 1  # the adapter itself never retries


def test_the_real_client_without_credentials_fails_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted: list[bool] = []

    def tripwire(**kwargs: Any) -> None:
        attempted.append(True)
        raise AssertionError("a request left the machine")

    # Nothing is configured (the autouse fixture cleared it), so signing fails first.
    # The tripwire on the network step guards against credentials ever resolving.
    client = REAL_NEW_CLIENT("us-east-1")
    client.meta.events.register("before-send.sns.Publish", tripwire)
    monkeypatch.setattr(sns_sender, "_new_client", lambda region: client)

    with pytest.raises(SendError) as exc:
        sns_email_sender()(ARN, SUBJECT, BODY)

    assert exc.value.reason == "no_credentials" and attempted == []


@pytest.mark.parametrize(
    "response",
    [{}, {"MessageId": ""}, {"MessageId": 123}, {"ResponseMetadata": {}}, None],
)
def test_a_success_without_a_message_id_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch, response: Any
) -> None:
    class Odd:
        def publish(self, **kwargs: Any) -> Any:
            return response

    monkeypatch.setattr(sns_sender, "_new_client", lambda region: Odd())

    with pytest.raises(SendError) as exc:
        sns_email_sender()(ARN, SUBJECT, BODY)

    assert exc.value.reason == "bad_response" and exc.value.retryable is False


def test_aws_error_text_and_identifiers_never_reach_errors_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stubber = stubbed(monkeypatch)
    stubber.add_client_error(
        "publish",
        service_error_code="AuthorizationError",
        service_message=f"User is not authorized to publish to {ARN} (account 123456789012)",
        http_status_code=403,
    )

    caplog.set_level(logging.DEBUG)
    with pytest.raises(SendError) as exc:
        send_notification(Message(body=BODY, source="ai"), ARN, sns_email_sender())

    shown = f"{exc.value!s} {exc.value!r} {exc.value.reason} {caplog.text}"
    assert "123456789012" not in shown and "not authorized" not in shown
    assert BODY not in shown
    assert exc.value.__cause__ is None and exc.value.__suppress_context__ is True


# --- problems with the user's own AWS setup, found while building the client ---


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ProfileNotFound(profile="nope"), "bad_aws_config"),
        (ConfigParseError(path="/x/config"), "bad_aws_config"),
        (InvalidConfigError(error_msg="bad value"), "bad_aws_config"),
        (InvalidRegionError(region_name="x y"), "bad_aws_config"),
        (NoRegionError(), "bad_aws_config"),
        (SSOTokenLoadError(error_msg="expired"), "no_credentials"),
        (UnauthorizedSSOTokenError(), "no_credentials"),
    ],
)
def test_setup_problems_raised_while_building_the_client_are_config_problems(
    monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str
) -> None:
    def failing_new_client(region: str) -> Any:
        raise error

    monkeypatch.setattr(sns_sender, "_new_client", failing_new_client)

    with pytest.raises(SendError) as exc:
        sns_email_sender()(ARN, SUBJECT, BODY)

    assert exc.value.reason == reason and exc.value.retryable is False


def _real_client_with_tripwire(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Use the REAL client factory, with a tripwire so nothing can reach a network."""
    attempted: list[bool] = []

    def real_new_client(region: str) -> Any:
        client = REAL_NEW_CLIENT(region)

        def tripwire(**kwargs: Any) -> None:
            attempted.append(True)
            raise AssertionError("a request left the machine")

        client.meta.events.register("before-send.sns.Publish", tripwire)
        return client

    monkeypatch.setattr(sns_sender, "_new_client", real_new_client)
    return attempted


@pytest.mark.parametrize("profile", ["definitely-not-a-profile", "default"])
def test_an_unknown_aws_profile_is_a_config_problem_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    # "default" matters: with only environment keys and no config files there is no
    # such profile, and botocore refuses it when the client is built.
    attempted = _real_client_with_tripwire(monkeypatch)
    monkeypatch.setenv("AWS_PROFILE", profile)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret-key")

    with pytest.raises(SendError) as exc:
        sns_email_sender()(ARN, SUBJECT, BODY)

    assert exc.value.reason == "bad_aws_config" and attempted == []


def test_environment_keys_alone_work_without_any_profile(
    monkeypatch: pytest.MonkeyPatch, aws_dummy_credentials: None
) -> None:
    client = REAL_NEW_CLIENT("us-east-1")
    monkeypatch.setattr(sns_sender, "_new_client", lambda region: client)
    stubber = Stubber(client)
    stubber.add_response("publish", {"MessageId": "m-1"})
    stubber.activate()

    assert sns_email_sender()(ARN, SUBJECT, BODY) == "m-1"


def test_a_malformed_aws_config_file_is_a_config_problem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    attempted = _real_client_with_tripwire(monkeypatch)
    broken = tmp_path / "config"
    broken.write_text("[default\nregion = us-east-1\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(broken))
    monkeypatch.setenv("AWS_PROFILE", "default")

    with pytest.raises(SendError) as exc:
        sns_email_sender()(ARN, SUBJECT, BODY)

    assert exc.value.reason == "bad_aws_config" and attempted == []
