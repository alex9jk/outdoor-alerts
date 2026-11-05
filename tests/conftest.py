from collections.abc import Callable

import httpx
import pytest

from gooddays import notify, sms_sender
from gooddays.fetch import http

# The real client factory, kept so adapter tests can opt back in (under respx).
REAL_NEW_CLIENT = sms_sender._new_client


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Skip real retry backoff; returns the delays that would have been slept."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(http, "_sleep", fake_sleep)
    return delays


@pytest.fixture(autouse=True)
def no_sms_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Skip the wait between SMS attempts; returns the waits that would have happened."""
    waits: list[float] = []
    monkeypatch.setattr(notify, "_sleep", waits.append)
    return waits


@pytest.fixture(autouse=True)
def no_real_sms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a real text message impossible from any test.

    The only place a real HTTP client for Twilio is made is `_new_client`; here it
    raises, so a test that forgets to fake the sender fails loudly instead of
    sending (and billing) a message. The settings a developer's shell or CI might
    carry are cleared too, so results never depend on the environment.
    """

    def forbidden(settings: sms_sender.TwilioSettings) -> httpx.Client:
        raise AssertionError("a test tried to make a real SMS client: fake the sender")

    monkeypatch.setattr(sms_sender, "_new_client", forbidden)
    for name in (
        "CI",
        "GOODDAYS_SMS_TO",
        "GOODDAYS_SMS_FROM",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def real_sms_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Opt a test back in to the real httpx client. Use only under `@respx.mock`."""

    def enable() -> None:
        monkeypatch.setattr(sms_sender, "_new_client", REAL_NEW_CLIENT)

    return enable
