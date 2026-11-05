import base64
import logging
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from conftest import REAL_NEW_CLIENT
from gooddays import sms_sender
from gooddays.models import Message
from gooddays.notify import SendError, send_notification
from gooddays.sms_sender import (
    ACCOUNT_SID_ENV,
    AUTH_TOKEN_ENV,
    FROM_ENV,
    MESSAGES_URL,
    TIMEOUT_SECONDS,
    TwilioSettings,
    load_twilio_settings,
    twilio_sender,
)
from gooddays.storage import ConfigError

SID = "ACtest0000000000000000000000000000"
TOKEN = "secret-token-value"
SETTINGS = TwilioSettings(account_sid=SID, auth_token=TOKEN, from_number="+15005550006")
TO = "+14155550123"
BODY = "Today Mon Jun 15:\nSpot 0 hiking: 65 F"
URL = MESSAGES_URL.format(sid=SID)

ENV = {ACCOUNT_SID_ENV: SID, AUTH_TOKEN_ENV: TOKEN, FROM_ENV: "+15005550006"}


@pytest.fixture
def sender(real_sms_client: Callable[[], None]) -> Callable[[str, str], str]:
    """The real adapter with the real httpx client; always used under respx."""
    real_sms_client()
    return twilio_sender(SETTINGS)


def created(sid: str = "SM123") -> httpx.Response:
    return httpx.Response(201, json={"sid": sid, "status": "queued"})


# --- settings ---


def test_settings_are_read_from_the_environment() -> None:
    settings = load_twilio_settings(ENV)

    assert settings == SETTINGS


def test_settings_are_trimmed() -> None:
    padded = {name: f"  {value}\n" for name, value in ENV.items()}

    assert load_twilio_settings(padded) == SETTINGS


@pytest.mark.parametrize("missing", [ACCOUNT_SID_ENV, AUTH_TOKEN_ENV, FROM_ENV])
@pytest.mark.parametrize("how", ["absent", "empty", "blank"])
def test_a_missing_setting_is_named_but_no_value_is_shown(
    missing: str, how: str
) -> None:
    env = dict(ENV)
    if how == "absent":
        del env[missing]
    else:
        env[missing] = "" if how == "empty" else "   "

    with pytest.raises(ConfigError) as exc:
        load_twilio_settings(env)

    assert missing in str(exc.value)
    assert TOKEN not in str(exc.value) and SID not in str(exc.value)


def test_every_missing_setting_is_listed_at_once() -> None:
    with pytest.raises(ConfigError) as exc:
        load_twilio_settings({})

    assert all(
        name in str(exc.value) for name in (ACCOUNT_SID_ENV, AUTH_TOKEN_ENV, FROM_ENV)
    )


def test_the_token_is_hidden_in_a_repr() -> None:
    assert TOKEN not in repr(SETTINGS) and TOKEN not in str(SETTINGS)


# --- the request ---


@respx.mock
def test_the_request_matches_twilios_messages_api(
    sender: Callable[[str, str], str],
) -> None:
    route = respx.post(URL).mock(return_value=created("SM777"))

    message_id = sender(TO, BODY)

    assert message_id == "SM777"
    (call,) = route.calls
    request = call.request
    assert str(request.url) == (
        f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"
    )
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    expected_auth = "Basic " + base64.b64encode(f"{SID}:{TOKEN}".encode()).decode()
    assert request.headers["authorization"] == expected_auth
    assert parse_qs(request.content.decode()) == {
        "To": [TO],
        "From": ["+15005550006"],
        "Body": [BODY],
    }


@respx.mock
def test_one_client_is_built_once_and_reused(
    real_sms_client: Callable[[], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[TwilioSettings] = []

    def counting(settings: TwilioSettings) -> httpx.Client:
        built.append(settings)
        return REAL_NEW_CLIENT(settings)

    monkeypatch.setattr(sms_sender, "_new_client", counting)
    respx.post(URL).mock(return_value=created())
    send = twilio_sender(SETTINGS)
    assert built == []  # nothing is built until the first message

    send(TO, BODY)
    send(TO, BODY)

    assert built == [SETTINGS]


def test_the_client_has_an_explicit_timeout_and_never_follows_redirects() -> None:
    client = REAL_NEW_CLIENT(SETTINGS)

    assert client.timeout == httpx.Timeout(TIMEOUT_SECONDS) and TIMEOUT_SECONDS == 10.0
    assert client.follow_redirects is False
    client.close()


def test_without_the_opt_in_a_real_client_cannot_be_made() -> None:
    # The autouse guard: forgetting to fake the sender fails loudly, never sends.
    with pytest.raises(AssertionError, match="fake the sender"):
        twilio_sender(SETTINGS)(TO, BODY)


# --- responses and errors: reason key and whether a retry is safe ---


@respx.mock
@pytest.mark.parametrize(
    ("status", "reason", "retryable"),
    [
        (500, "server_error", True),
        (502, "server_error", True),
        (503, "server_error", True),
        (401, "auth_rejected", False),
        (403, "auth_rejected", False),
        (429, "rate_limited", False),
        (400, "rejected", False),
        (404, "rejected", False),
        (422, "rejected", False),
        (302, "bad_response", False),
    ],
)
def test_http_statuses_map_to_a_reason_and_a_retry_decision(
    sender: Callable[[str, str], str], status: int, reason: str, retryable: bool
) -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(status, text="no json here")
    )

    with pytest.raises(SendError) as exc:
        sender(TO, BODY)

    assert exc.value.reason == reason
    assert exc.value.retryable is retryable
    assert route.call_count == 1  # the adapter itself never retries


@respx.mock
@pytest.mark.parametrize(
    ("error", "reason", "retryable"),
    [
        (httpx.ConnectError("refused"), "connection_error", True),
        (httpx.ConnectTimeout("slow"), "connect_timeout", True),
        (httpx.ReadTimeout("slow"), "read_timeout", False),
        (httpx.WriteTimeout("slow"), "read_timeout", False),
        (httpx.PoolTimeout("busy"), "read_timeout", False),
        (httpx.RemoteProtocolError("hung up"), "request_failed", False),
        (httpx.ReadError("reset"), "request_failed", False),
    ],
)
def test_network_errors_map_to_a_reason_and_a_retry_decision(
    sender: Callable[[str, str], str], error: Exception, reason: str, retryable: bool
) -> None:
    route = respx.post(URL).mock(side_effect=error)

    with pytest.raises(SendError) as exc:
        sender(TO, BODY)

    assert exc.value.reason == reason and exc.value.retryable is retryable
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"status": "queued"}),  # no sid
        httpx.Response(200, json={"sid": ""}),
        httpx.Response(200, json={"sid": 12345}),
        httpx.Response(200, json=["sid"]),
        httpx.Response(204),
    ],
    ids=["not json", "no sid", "empty sid", "numeric sid", "list", "empty body"],
)
def test_a_success_without_a_message_id_is_not_accepted(
    sender: Callable[[str, str], str], response: httpx.Response
) -> None:
    respx.post(URL).mock(return_value=response)

    with pytest.raises(SendError) as exc:
        sender(TO, BODY)

    assert exc.value.reason == "bad_response" and exc.value.retryable is False


@respx.mock
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            {"code": 21211, "message": "The 'To' number +14155550123 is not valid"},
            "rejected_21211",
        ),
        ({"code": 21608}, "rejected_21608"),
        ({"message": "no code"}, "rejected"),
        ({"code": "21211"}, "rejected"),  # only a real integer is used
        ({"code": True}, "rejected"),
        ({"code": None}, "rejected"),
    ],
)
def test_a_rejection_adds_twilios_numeric_code_as_a_hint(
    sender: Callable[[str, str], str], body: dict[str, object], reason: str
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(400, json=body))

    with pytest.raises(SendError) as exc:
        sender(TO, BODY)

    assert exc.value.reason == reason


@respx.mock
def test_provider_error_text_and_secrets_never_reach_errors_or_logs(
    sender: Callable[[str, str], str], caplog: pytest.LogCaptureFixture
) -> None:
    leaky = {
        "code": 21211,
        "message": f"The 'To' number {TO} is not valid",
        "more_info": TOKEN,
    }
    respx.post(URL).mock(return_value=httpx.Response(400, json=leaky))

    # The CLI holds httpx at WARNING (it logs every request URL, which contains the
    # account SID); mirror that here.
    caplog.set_level(logging.WARNING, logger="httpx")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SendError) as exc:
        send_notification(Message(body=BODY, source="ai"), TO, sender)

    shown = f"{exc.value!s} {exc.value!r} {exc.value.reason} {caplog.text}"
    assert TO not in shown and "5550123" not in shown
    assert TOKEN not in shown and SID not in shown
    assert "not valid" not in shown
    assert exc.value.__cause__ is None  # no chained exception that could carry the text


# --- with the retry loop in notify.py ---


@respx.mock
def test_a_5xx_then_success_sends_one_text_in_two_requests(
    sender: Callable[[str, str], str], no_sms_sleep: list[float]
) -> None:
    route = respx.post(URL).mock(side_effect=[httpx.Response(503), created("SM42")])

    result = send_notification(Message(body=BODY, source="ai"), TO, sender)

    assert result.accepted and result.message_id == "SM42" and result.attempts == 2
    assert route.call_count == 2 and len(no_sms_sleep) == 1


@respx.mock
def test_a_read_timeout_is_not_retried_so_it_cannot_text_twice(
    sender: Callable[[str, str], str], no_sms_sleep: list[float]
) -> None:
    route = respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(SendError) as exc:
        send_notification(Message(body=BODY, source="ai"), TO, sender)

    assert exc.value.reason == "read_timeout" and exc.value.attempts == 1
    assert route.call_count == 1 and no_sms_sleep == []


@respx.mock
def test_a_connect_failure_is_retried_once(
    sender: Callable[[str, str], str],
) -> None:
    route = respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(SendError) as exc:
        send_notification(Message(body=BODY, source="ai"), TO, sender)

    assert exc.value.reason == "connection_error" and exc.value.attempts == 2
    assert route.call_count == 2
