---
name: build-with-tests
description: Build, implement, add, or extend a feature in this project (the gooddays Python package), with unit tests written alongside the code. Use whenever the user asks to build, implement, add, create, or extend a module, API client, model, CLI command, or other feature. It covers reading CLAUDE.md and the technical brief first, matching 2-3 existing similar modules, writing tests as part of normal engineering, and finishing with ruff, mypy, and pytest.
---

# Build a production feature

Follow this workflow for any feature work. Its goal is code that looks like the rest of the project and ships with passing checks.

## 1. Read before you write
- Read `CLAUDE.md` in full. It sets the stack, non-goals, conventions, and guardrails. A feature that breaks a rule is wrong even if it works. In particular:
  - Only build the stage named under "Current focus". Don't build later stages early.
  - No AWS dependencies yet.
  - Don't add dependencies without asking the user first.
  - No hard-coded locations, thresholds, or settings; they live in `spots.yaml`.
  - `fetch_conditions` only fetches and normalizes; it does no scoring or judgment.
- Read the technical brief or task description. If there is none, restate the requirements in a few lines and confirm anything ambiguous before coding.
- Note the acceptance criteria. These decide which tests you write.

## 2. Study 2-3 similar modules
- Find the existing code closest to the one you're building (Glob/Grep under `src/gooddays/` and `tests/`). The `codebase-researcher` subagent can do this if the area is large.
- Read them end to end: structure, naming, type hints, how errors are raised or recorded, how tests are laid out. Good references right now: `models.py` (frozen, slotted dataclasses) and `storage.py` with `tests/test_storage.py` (validation and error tests).
- Match what you find. If the codebase has no close analogue, say so and follow `CLAUDE.md`. Don't invent a new pattern when an existing one fits.

## 3. Build with tests alongside
- Write unit tests as you go, in the same change as the code. This is ordinary good engineering, not a red-green TDD loop. Test as soon as a piece is shaped enough to test.
- Test behavior and edge cases (empty input, missing fields, invalid values, failure paths), not implementation details.
- Put testable logic (normalization, validation, formatting) in plain, type-hinted Python functions, so tests need no network and no I/O beyond `tmp_path`.
- Keep modules small and compose them.

## 4. Verify before reporting done
Run these from the repo root, in order, and fix failures at the root cause:
1. `uv run ruff check .`: lint
2. `uv run ruff format --check .`: formatting (`uv run ruff format .` fixes it)
3. `uv run mypy src`: strict type check
4. `uv run pytest`: unit tests

There is no build step. Report the real results. If a check fails or you skipped one, say so. Don't claim success without running them.

## 5. Project conventions
From `CLAUDE.md` and the existing code:
- Python 3.12, managed with `uv`; fully type-hinted and `mypy --strict` clean on `src/`.
- `httpx` with explicit timeouts (never `requests`); stdlib dataclasses for models.
- Retries: up to 3 attempts with exponential backoff on timeouts and 5xx only; never retry 4xx. Timeout is 10s per request; concurrency is capped at 5.
- One bad spot never fails the run: catch per-spot errors, record a `SpotError`, and keep going.
- Log with the standard `logging` module in key=value style, never `print`.
- Tests use `pytest` with `respx` for mocked HTTP; tests never hit the network. Async tests rely on `asyncio_mode = "auto"`.
- Never commit secrets or contact details. The NWS User-Agent comes from the `GOODDAYS_NWS_USER_AGENT` environment variable or `settings.nws_user_agent`.
- Don't delete fixtures or data files without asking, and don't run deploy commands.

Established patterns:
- **Naming:** snake_case modules, functions, and variables; PascalCase classes; spot ids are lowercase slugs like `bear-mountain`.
- **Error handling:** config problems raise `ConfigError` with a message naming the exact field (see `storage.py`); fetch failures are recorded as `SpotError`.
- **Where business logic lives:** API clients in `src/gooddays/fetch/` stay thin and return parsed raw responses; normalization lives in `fetch/service.py` so it can be tested from fixtures alone; config access goes through the `Storage` protocol.
- **Test structure:** `tests/test_<module>.py`; saved raw API responses in `tests/fixtures/`; `tmp_path` for files, `monkeypatch` for environment variables, `parametrize` for case tables, and `pytest.raises(..., match=...)` for specific error messages.
