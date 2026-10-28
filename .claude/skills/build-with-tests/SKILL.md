---
name: build-with-tests
description: Build, implement, add, or extend a feature in this project (the gooddays Python package), with unit tests written alongside the code. Use whenever the user asks to build, implement, add, create, or extend a module, API client, model, CLI command, or other feature. It covers reading CLAUDE.md first, matching 2-3 existing similar modules, working on a feature branch, writing tests as part of normal engineering, and finishing with ruff, mypy, and pytest.
---

# Build a feature in gooddays

Follow this workflow for any feature work. Its goal is code that looks like the rest of the project and ships with passing checks.

## 1. Read before you write
- Read `CLAUDE.md` in full. It sets the pipeline stages, current focus, conventions, and rules. A feature that breaks a rule is wrong even if it works. In particular:
  - Only build the stage named under "Current focus". Don't build later stages early.
  - No AWS dependencies yet.
  - Don't add dependencies without asking the user first.
  - No hard-coded locations, thresholds, or settings; they live in `spots.yaml`.
  - `fetch_conditions` does no scoring or judgment.
- Read the task description or technical brief. If there is none, restate the requirements in a few lines and confirm anything ambiguous before coding.
- Note the acceptance criteria. These decide which tests you write.

## 2. Work on a feature branch
- Branch off `develop` (e.g. `feature/<name>`). Push only to the feature branch, never directly to `develop` or `main`.
- Pull requests go from the feature branch into `develop`.
- If the work depends on an unmerged branch, say so and stack on it deliberately. Stay on the branch the user is already on unless they say otherwise.

## 3. Study 2-3 similar modules
- Find the closest existing code with Glob/Grep under `src/gooddays/` and `tests/`. The `codebase-researcher` subagent can do this if the area is large.
- Read them end to end: structure, naming, type hints, how errors are raised or recorded, how tests are laid out. Good references right now: `models.py` (frozen, slotted dataclasses) and `storage.py` plus `tests/test_storage.py` (validation and error tests).
- Match what you find. If there is no close analogue, follow `CLAUDE.md`. Don't invent a new pattern when an existing one fits.

## 4. Build with tests alongside
- Work in small steps and stop after each for the user to review the diff.
- Write tests in the same change as the code, as soon as a piece is shaped enough to test. This is ordinary good engineering, not a red-green TDD loop.
- Test behavior and edge cases (missing fields, invalid input, failure paths), not implementation details.
- Keep logic testable: pure functions for normalization and validation, so tests need no network and no I/O beyond `tmp_path`.
- Explain any design decision that isn't obvious. The user must be able to defend every choice in an interview.

## 5. Verify before reporting done
Run these from the repo root and fix failures at the root cause:
1. `uv run ruff check .`: lint
2. `uv run ruff format --check .`: formatting (run `uv run ruff format .` to fix)
3. `uv run mypy src`: strict type check
4. `uv run pytest`: unit tests

There is no build step. Report the real results. If a check fails or you skipped one, say so. Don't claim success without running them.

## 6. Project conventions
From `CLAUDE.md` and the existing code:
- **Stack:** Python 3.12, `uv`, `httpx` with explicit timeouts (never `requests`), stdlib dataclasses for models, fully type-hinted (`mypy --strict` on `src/`).
- **Naming:** snake_case modules, functions, and variables; PascalCase classes; spot ids are lowercase slugs like `bear-mountain` (they appear in log lines and `data/raw/` paths).
- **Error handling:**
  - Config problems raise `ConfigError` (see `storage.py`) with a message that names the exact field.
  - Per-spot fetch failures never fail the run: catch them, record a `SpotError`, and keep going.
  - Retry up to 3 attempts with exponential backoff on timeouts and 5xx only. Never retry 4xx.
  - Timeout is 10s per request, and concurrency is capped at 5.
- **Where logic lives:** API clients in `src/gooddays/fetch/` stay thin and return parsed raw responses. Normalization happens in `fetch/service.py` so it can be tested from fixtures alone. Models live in `models.py`, and config access goes through the `Storage` protocol in `storage.py`.
- **Logging:** standard `logging` module, key=value style. Never `print`.
- **Tests:**
  - Files are `tests/test_<module>.py`, with type-hinted test functions and `pytest.mark.parametrize` for case tables.
  - Raw API fixtures are saved JSON under `tests/fixtures/`.
  - HTTP is mocked with `respx`; tests never hit the network.
  - Async tests rely on `asyncio_mode = "auto"`, so no per-test decorator is needed.
  - Use `tmp_path` for files and `monkeypatch` for environment variables.
  - Assert specific error messages with `pytest.raises(..., match=...)`.
- **Secrets and contact details:** never commit them. The NWS User-Agent comes from the `GOODDAYS_NWS_USER_AGENT` environment variable or `settings.nws_user_agent`. Don't commit `.env`, key, or credential files (the pre-commit hook blocks them).
- Don't delete user data or fixtures without asking, and don't run deploy commands.
