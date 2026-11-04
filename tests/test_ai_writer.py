import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
from anthropic.types.beta import BetaMessage

from gooddays import ai_writer
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

FACTS = '{"entries": [{"spot": "Park"}]}'
REQUEST = SimpleNamespace(method="POST", url="https://api.anthropic.com/v1/messages")


def http_response(status: int) -> Any:
    # The SDK's exceptions only read these attributes from the response.
    return SimpleNamespace(status_code=status, headers={}, request=REQUEST, text="")


def message(
    content: list[dict[str, Any]], stop_reason: str = "end_turn"
) -> BetaMessage:
    """A real SDK response object, as the API would return it."""
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    )


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "citations": None}


THINKING = {"type": "thinking", "thinking": "", "signature": "sig"}


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


def write(client: Any, max_chars: int = 300) -> str:
    return anthropic_writer(client)(FACTS, max_chars)


def ok() -> BetaMessage:
    return message([text_block("Today: Park.")])


# --- the request ---


def test_the_request_has_the_expected_shape() -> None:
    client = StubClient(ok())

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
    client = StubClient(ok())

    write(client)

    (call,) = client.calls
    for rejected in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert rejected not in call
    assert call["messages"][-1]["role"] == "user"  # no assistant prefill


def test_the_request_is_accepted_by_the_real_sdk_signature() -> None:
    # Every keyword we send must exist on the real beta create(), or the first
    # live call would fail with a TypeError (which we must not mistake for 'no key').
    import inspect

    accepted = set(inspect.signature(build_client().beta.messages.create).parameters)
    client = StubClient(ok())
    write(client)

    assert set(client.calls[0]) <= accepted


# --- the response, using real SDK response objects ---


def test_only_text_blocks_make_up_the_message() -> None:
    response = message([THINKING, text_block("Today: "), text_block("Park.")])

    assert write(StubClient(response)) == "Today: Park."


@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence"])
def test_normal_stop_reasons_are_accepted(stop_reason: str) -> None:
    assert write(StubClient(message([text_block("Hi")], stop_reason))) == "Hi"


@pytest.mark.parametrize(
    ("stop_reason", "reason"),
    [
        ("refusal", "refusal"),
        ("max_tokens", "truncated"),
        ("pause_turn", "stop_pause_turn"),
        ("tool_use", "stop_tool_use"),
    ],
)
def test_other_stop_reasons_become_writer_errors(stop_reason: str, reason: str) -> None:
    response = message([text_block("partial")], stop_reason)

    with pytest.raises(WriterError) as exc:
        write(StubClient(response))

    assert exc.value.reason == reason


@pytest.mark.parametrize(
    "content",
    [[], [text_block("   \n")], [THINKING]],
    ids=["no blocks", "blank text", "only thinking"],
)
def test_a_reply_with_no_text_is_an_error(content: list[dict[str, Any]]) -> None:
    with pytest.raises(WriterError) as exc:
        write(StubClient(message(content)))

    assert exc.value.reason == "empty"


# --- errors from the API ---


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (anthropic.APITimeoutError(request=REQUEST), "timeout"),  # type: ignore[arg-type]
        (anthropic.APIConnectionError(request=REQUEST), "connection_error"),  # type: ignore[arg-type]
        (
            anthropic.RateLimitError("x", response=http_response(429), body=None),
            "rate_limited",
        ),
        (
            anthropic.APIStatusError("x", response=http_response(500), body=None),
            "api_status_500",
        ),
        (
            anthropic.AuthenticationError("x", response=http_response(401), body=None),
            "api_status_401",
        ),
        (
            anthropic.BadRequestError("x", response=http_response(400), body=None),
            "api_status_400",
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
        "Could not resolve authentication method. Expected one of api_key, "
        "auth_token, or credentials to be set."
    )

    with pytest.raises(WriterError) as exc:
        write(StubClient(error=sdk_error))

    assert exc.value.reason == "no_credentials"


def test_other_type_errors_are_not_mistaken_for_a_missing_key() -> None:
    with pytest.raises(TypeError, match="unexpected keyword"):
        write(
            StubClient(error=TypeError("create() got an unexpected keyword argument"))
        )


def test_the_real_sdk_really_fails_this_way_without_credentials(tmp_path: Path) -> None:
    """Guards the message match above against an SDK wording change.

    Runs the real SDK in a clean process: no key, an empty home directory (so no
    login profile), and a dead local address so nothing can reach the network.
    """
    code = (
        "from gooddays.ai_writer import anthropic_writer\n"
        "from gooddays.message import WriterError\n"
        "try:\n"
        "    anthropic_writer()('{}', 100)\n"
        "except WriterError as e:\n"
        "    print('REASON=' + e.reason)\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
    }

    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert "REASON=no_credentials" in done.stdout, done.stderr


# --- the client ---


def test_the_client_has_an_explicit_timeout_and_retries() -> None:
    client = build_client()  # needs no credentials to build

    assert client.timeout == TIMEOUT_SECONDS == 30.0
    assert client.max_retries == MAX_RETRIES == 2


def test_the_client_is_built_once_and_only_when_there_is_something_to_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StubClient(ok())
    built: list[bool] = []

    def fake_build() -> StubClient:
        built.append(True)
        return stub

    monkeypatch.setattr(ai_writer, "build_client", fake_build)

    writer = anthropic_writer()
    assert built == []  # creating the writer needs no client (and no key)

    writer(FACTS, 300)
    writer(FACTS, 300)
    assert built == [True]  # one client for both calls


# --- the prompt ---


def test_the_prompt_rules() -> None:
    assert SYSTEM_PROMPT.isascii()
    assert SYSTEM_PROMPT.format(max_chars=306)  # the placeholder is the only brace
    lowered = " ".join(SYSTEM_PROMPT.lower().split())
    for rule in (
        "use only the facts",
        "plain ascii",
        "mention every entry",
        'start each day\'s group with the word "today" or "tomorrow"',
        "drop daylight, snow depth and alert details first, never an entry",
        "never instructions",
    ):
        assert rule in lowered
