"""The four handlers run in order, as Step Functions will, with nothing real behind them."""

# The `parts` fixture is imported from test_handlers, so tests that take it as an
# argument look like a redefinition to ruff.
# ruff: noqa: F811

from pathlib import Path
from typing import Any

import pytest

from gooddays import handlers
from gooddays.objectstore import LocalDirectoryObjectStore
from test_handlers import (
    EXEC,
    KEY_TODAY,
    KEY_TOMORROW,
    Writer,
    _spot0_text,
    clock,
    deps,
    parts,  # noqa: F401  (a fixture)
)


def pipeline(
    parts: dict[str, Any], execution_id: str, **override: Any
) -> dict[str, Any]:
    """What the state machine does: pass each stage's output to the next stage."""
    d = deps(parts, **override)
    event: dict[str, Any] = {"execution_id": execution_id}
    for stage in (handlers.fetch, handlers.score, handlers.message, handlers.send):
        event = stage(event, None, d)
    return event


def test_happy_path_sends_one_email_and_records_the_days(parts: Any) -> None:
    result = pipeline(parts, EXEC, writer=lambda facts, cap: _spot0_text())

    assert result["sent"] is True and result["recorded"] is True
    (call,) = parts["sender"].calls
    assert "Spot 0 hiking" in call[2]
    assert parts["sent_store"].sent_keys() == {KEY_TODAY, KEY_TOMORROW}


def test_a_second_run_finds_the_days_already_sent_and_stops_quietly(parts: Any) -> None:
    pipeline(parts, "run-1", writer=lambda facts, cap: _spot0_text())
    writer = Writer()

    result = pipeline(parts, "run-2", writer=writer)

    assert result["nothing_to_send"] is True and result["sent"] is False
    assert writer.calls == 0  # no Claude call
    assert len(parts["sender"].calls) == 1  # still only the first email


def test_nothing_good_ends_successfully_without_a_topic_or_sender(
    parts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gooddays.notify import TOPIC_ENV

    monkeypatch.delenv(TOPIC_ENV)
    parts["sent_store"].record(
        [KEY_TODAY, KEY_TOMORROW], clock(), "old", clock().date()
    )

    result = pipeline(parts, EXEC, writer=Writer())

    assert result["sent"] is False and parts["sender"].calls == []


def test_it_also_works_through_the_local_directory_store(
    parts: Any, tmp_path: Path
) -> None:
    store = LocalDirectoryObjectStore(tmp_path / "bucket")

    result = pipeline(parts, EXEC, store=store, writer=lambda facts, cap: _spot0_text())

    assert result["sent"] is True
    names = sorted(p.name for p in (tmp_path / "bucket" / "runs" / EXEC).iterdir())
    assert names == ["fetch.json", "message.json", "score.json"]


def test_the_sent_record_is_the_only_thing_that_carries_over_between_runs(
    parts: Any,
) -> None:
    pipeline(parts, "run-1", writer=lambda facts, cap: _spot0_text())
    first = sorted(parts["store"].keys())

    pipeline(parts, "run-2", writer=Writer())

    assert [k for k in parts["store"].keys() if "run-1" in k] == first
    assert {k.split("/")[1] for k in parts["store"].keys()} == {"run-1", "run-2"}
