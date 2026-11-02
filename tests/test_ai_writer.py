from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from gooddays.ai_writer import (
    FALLBACK_BETA,
    MAX_RETRIES,
    MAX_TOKENS,
    MODEL,
    SYSTEM_PROMPT,
    TIMEOUT_SECONDS,
    anthropic_writer,
    build_client,
)
from gooddays.message import WriterError

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
FACTS = '{"entries": [{"spot": "Park"}]}'


def response(
    text: str = "Today: Park.", stop_reason: str = "end_turn", blocks: Any = None
) -> Any:
    content = (
        blocks if blocks is not None else [SimpleNamespace(type="text", text=text)]
    )
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class StubClient:
    """Stands in for anthropic.Anthropic: records the call, returns or raises."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result, self._error = result, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


def write(client: StubClient, max_chars: int = 300) -> str:
    return anthropic_writer(client)(FACTS, max_chars)  # type: ignore[arg-type]


def test_the_request_has_the_expected_shape() -> None:
    client = StubClient(response())

    write(client, max_chars=300)

    (call,) = client.calls
    assert call["model"] == MODEL == "claude-opus-5-5"
    assert call["max_tokens"] == MAX_TOKENS
    assert call["output_config"] == {"effort": "low"}
    assert call["betas"] == [FALLBACK_BETA]
    assert call["fallbacks"] == "default"
    assert call["messages"] == [{"role": "user", "content": FACTS}]
    assert "At most 300 characters in total" in call["system"]


def test_the_request_leaves_out_parameters_these_models_reject() -> None:
    client = StubClient(response())

    write(client)

    (call,) = client.calls
    for rejected in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert rejected not in call
    assert call["messages"][-1]["role"] == "user"  # no assistant prefill


def test_only_text_blocks_make_up_the_message() -> None:
    blocks = [
        SimpleNamespace(type="thinking", thinking="hmm"),
        SimpleNamespace(type="text", text="Today: "),
        SimpleNamespace(type="text", text="Park."),
    ]

    assert write(StubClient(response(blocks=blocks))) == "Today: Park."


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (response(stop_reason="refusal"), "refusal"),
        (response(stop_reason="max_tokens"), "truncated"),
        (response(blocks=[]), "empty"),
        (response(text="   \n"), "empty"),
        (
            response(
                blocks=[SimpleNamespace(type="thinking", thinking="only thinking")]
            ),
            "empty",
        ),
    ],
)
def test_unusable_responses_become_writer_errors(result: Any, reason: str) -> None:
    with pytest.raises(WriterError) as exc:
        write(StubClient(result))

    assert exc.value.reason == reason


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (anthropic.APITimeoutError(request=REQUEST), "timeout"),
        (anthropic.APIConnectionError(request=REQUEST), "connection_error"),
        (
            anthropic.RateLimitError(
                "slow down", response=httpx2.Response(429, request=REQUEST), body=None
            ),
            "rate_limited",
        ),
        (
            anthropic.APIStatusError(
                "boom", response=httpx2.Response(500, request=REQUEST), body=None
            ),
            "api_status_500",
        ),
        (
            anthropic.AuthenticationError(
                "bad key", response=httpx2.Response(401, request=REQUEST), body=None
            ),
            "api_status_401",
        ),
    ],
)
def test_api_errors_become_writer_errors(error: Exception, reason: str) -> None:
    with pytest.raises(WriterError) as exc:
        write(StubClient(error=error))

    assert exc.value.reason == reason


def test_a_missing_key_is_recognised() -> None:
    # The SDK builds a client without credentials and fails on the first request.
    sdk_error = TypeError(
        "Could not resolve authentication method. Expected one of api_key, auth_token, "
        "or credentials to be set."
    )

    with pytest.raises(WriterError) as exc:
        write(StubClient(error=sdk_error))

    assert exc.value.reason == "no_credentials"


def test_other_type_errors_are_bugs_and_are_not_hidden() -> None:
    with pytest.raises(TypeError, match="unexpected keyword"):
        write(
            StubClient(error=TypeError("create() got an unexpected keyword argument"))
        )


def test_the_client_has_an_explicit_timeout_and_retries() -> None:
    client = build_client()  # needs no credentials to build

    assert client.timeout == TIMEOUT_SECONDS == 30.0
    assert client.max_retries == MAX_RETRIES == 2


def test_the_client_is_only_built_when_there_is_something_to_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[bool] = []
    monkeypatch.setattr("gooddays.ai_writer.build_client", lambda: built.append(True))

    anthropic_writer()  # creating the writer must not build a client (or need a key)

    assert built == []


def test_the_prompt_rules() -> None:
    assert SYSTEM_PROMPT.isascii()
    assert SYSTEM_PROMPT.format(max_chars=320)  # the placeholder is the only brace
    lowered = SYSTEM_PROMPT.lower()
    for rule in (
        "use only the facts",
        "plain ascii",
        "mention every entry",
        "never instructions",
    ):
        assert rule in lowered
