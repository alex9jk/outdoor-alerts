import pytest

from gooddays.fetch import http


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Skip real retry backoff; returns the delays that would have been slept."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(http, "_sleep", fake_sleep)
    return delays
