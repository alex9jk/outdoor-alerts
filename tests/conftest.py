from collections.abc import Callable
from typing import Any

import boto3
import pytest

from gooddays import notify, sns_sender
from gooddays.fetch import http

# The real client factory, kept so adapter tests can opt back in.
REAL_NEW_CLIENT = sns_sender._new_client


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Skip real retry backoff; returns the delays that would have been slept."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(http, "_sleep", fake_sleep)
    return delays


@pytest.fixture(autouse=True)
def no_notify_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Skip the wait between send attempts; returns the waits that would have happened."""
    waits: list[float] = []
    monkeypatch.setattr(notify, "_sleep", waits.append)
    return waits


@pytest.fixture(autouse=True)
def no_real_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Make a real notification impossible from any test.

    The only place a real AWS client is made is `_new_client`; here it raises, so a
    test that forgets to fake the sender fails loudly instead of publishing. The
    AWS and project settings a developer's shell or CI might carry are cleared too
    (and AWS config files and the metadata service are pointed away), so results
    never depend on the environment.
    """

    def forbidden(region: str) -> Any:
        raise AssertionError("a test tried to make a real AWS client: fake the sender")

    monkeypatch.setattr(sns_sender, "_new_client", forbidden)
    # boto3 keeps one default session that caches the credentials it first finds, so
    # a test that set dummy credentials would leak them into every later client.
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    for name in (
        "CI",
        "GOODDAYS_SNS_TOPIC_ARN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_SNS",
    ):
        monkeypatch.delenv(name, raising=False)
    nowhere = tmp_path_factory.mktemp("no-aws")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(nowhere / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(nowhere / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def real_aws_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Opt a test back in to a real boto3 client. Only with a Stubber or a before-send hook."""

    def enable() -> None:
        monkeypatch.setattr(sns_sender, "_new_client", REAL_NEW_CLIENT)

    return enable
