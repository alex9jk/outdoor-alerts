"""Shared GET-with-retries helper used by the thin API clients.

Keeping the retry policy in one place means each client is just "build the
request, return the parsed JSON", and the policy is tested once.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

Source = Literal["open_meteo", "nws"]

# Called with (spot_id, source, raw parsed JSON) for every successful response.
RawCallback = Callable[[str, Source, dict[str, Any]], None]

TIMEOUT_SECONDS = 10.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5  # waits 0.5s, then 1s, between the 3 attempts

# Indirection so tests can skip the real sleeps.
_sleep = asyncio.sleep


class FetchError(Exception):
    """A request failed for good (retries exhausted, 4xx, or a bad response).

    The service turns this into a `SpotError`, adding the spot id.
    """

    def __init__(
        self, source: Source, message: str, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.source = source
        self.message = message
        self.status_code = status_code


async def get_json(
    client: httpx.AsyncClient,
    source: Source,
    url: str,
    *,
    params: Mapping[str, str | int] | None = None,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """GET `url` and return the parsed JSON object.

    Retries (up to MAX_ATTEMPTS, exponential backoff) on timeouts and 5xx only.
    4xx and other failures are not retried.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.get(
                url, params=params, headers=headers, timeout=TIMEOUT_SECONDS
            )
        except httpx.TimeoutException as e:
            failure = FetchError(source, f"timeout: {type(e).__name__}")
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as e:
            # Connection errors, a request that can't be built (e.g. a non-ASCII
            # header value), etc.: not in the retry policy, fail this spot.
            raise FetchError(source, f"request failed: {type(e).__name__}: {e}") from e
        else:
            status = response.status_code
            if 200 <= status < 300:
                return _parse_json_object(source, response)
            # Redirects aren't followed, so 3xx is a failure like any other 4xx.
            failure = FetchError(source, f"HTTP {status}", status)
            if status < 500:
                logger.warning(
                    "fetch_failed source=%s status=%d retry=no", source, status
                )
                raise failure

        will_retry = attempt < MAX_ATTEMPTS
        logger.warning(
            "fetch_failed source=%s attempt=%d error=%r retry=%s",
            source,
            attempt,
            failure.message,
            "yes" if will_retry else "no",
        )
        if not will_retry:
            raise failure
        await _sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))

    raise AssertionError("unreachable")  # pragma: no cover


def notify_raw(
    on_raw: RawCallback | None, spot_id: str, source: Source, raw: dict[str, Any]
) -> None:
    """Hand a raw response to the callback; its failures never fail the spot."""
    if on_raw is None:
        return
    try:
        on_raw(spot_id, source, raw)
    except Exception as e:
        logger.warning(
            "raw_callback_failed spot=%s source=%s error=%s: %s",
            spot_id,
            source,
            type(e).__name__,
            e,
        )


def _parse_json_object(source: Source, response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as e:
        raise FetchError(source, "response was not valid JSON") from e
    if not isinstance(data, dict):
        raise FetchError(source, "response JSON was not an object")
    return data
