"""The Twilio SMS sender: one httpx POST per message, behind `SmsSender`.

Swapping vendors means writing another adapter that returns the same `SmsSender`;
nothing else changes. Everything here is checked against Twilio's Messages API
docs (https://www.twilio.com/docs/sms/api/message): a form-encoded POST of
To, From and Body to the account's Messages.json, HTTP Basic auth with the
account SID and auth token, and a JSON reply with a `sid` and a `status`
(normally `queued`, which means accepted, not delivered).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from gooddays.notify import SendError, SmsSender
from gooddays.storage import ConfigError

ACCOUNT_SID_ENV = "TWILIO_ACCOUNT_SID"
AUTH_TOKEN_ENV = "TWILIO_AUTH_TOKEN"
FROM_ENV = "GOODDAYS_SMS_FROM"

MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class TwilioSettings:
    account_sid: str
    auth_token: str
    from_number: str

    def __repr__(self) -> str:  # never let the token reach a log by accident
        return f"TwilioSettings(account_sid={self.account_sid!r}, auth_token='***')"


def load_twilio_settings(env: Mapping[str, str] | None = None) -> TwilioSettings:
    """Read the credentials and sending number, naming every missing setting."""
    source = os.environ if env is None else env
    names = (ACCOUNT_SID_ENV, AUTH_TOKEN_ENV, FROM_ENV)
    values = {name: source.get(name, "").strip() for name in names}
    missing = [name for name in names if not values[name]]
    if missing:
        raise ConfigError("set " + ", ".join(missing) + " to send a text message")
    return TwilioSettings(
        account_sid=values[ACCOUNT_SID_ENV],
        auth_token=values[AUTH_TOKEN_ENV],
        from_number=values[FROM_ENV],
    )


def _new_client(settings: TwilioSettings) -> httpx.Client:
    """The one place a real HTTP client is made (tests replace it)."""
    return httpx.Client(
        auth=(settings.account_sid, settings.auth_token),
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
    )


def twilio_sender(settings: TwilioSettings) -> SmsSender:
    """An `SmsSender` that texts through Twilio. The client is built on first use."""
    clients: list[httpx.Client] = []

    def send(to: str, body: str) -> str:
        if not clients:
            clients.append(_new_client(settings))
        url = MESSAGES_URL.format(sid=settings.account_sid)
        data = {"To": to, "From": settings.from_number, "Body": body}
        try:
            response = clients[0].post(url, data=data)
        except httpx.ConnectTimeout:
            raise SendError("connect_timeout", retryable=True) from None
        except httpx.ConnectError:
            raise SendError("connection_error", retryable=True) from None
        except httpx.TimeoutException:
            # The request may have left: a retry could text twice.
            raise SendError("read_timeout") from None
        except httpx.HTTPError:
            raise SendError("request_failed") from None
        return _message_id(response)

    return send


def _message_id(response: httpx.Response) -> str:
    """The Twilio message id, or a SendError. Response text is never kept."""
    status = response.status_code
    if 200 <= status < 300:
        try:
            sid = response.json().get("sid")
        except (ValueError, AttributeError):
            sid = None
        if isinstance(sid, str) and sid:
            return sid
        raise SendError("bad_response")
    if status >= 500:
        raise SendError("server_error", retryable=True)
    if status in (401, 403):
        raise SendError("auth_rejected")
    if status == 429:
        raise SendError("rate_limited")
    if 300 <= status < 400:
        raise SendError("bad_response")  # redirects are not followed
    raise SendError(_rejected_reason(response))


def _rejected_reason(response: httpx.Response) -> str:
    """`rejected`, plus Twilio's numeric error code when there is one.

    The code is only a diagnostic hint in the reason (for example an invalid
    number or an unverified trial number); we never branch on it, and we never
    keep Twilio's error text, which can contain the phone number.
    """
    try:
        code = response.json().get("code")
    except (ValueError, AttributeError):
        return "rejected"
    return (
        f"rejected_{code}"
        if isinstance(code, int) and not isinstance(code, bool)
        else "rejected"
    )
