# CLAUDE.md — fetch_conditions

## Project context
"Is it a good day?" is a personal app that checks weather conditions for my favorite
outdoor spots (hiking, biking, skiing) and alerts me when a day looks good.

The pipeline has four stages:
`fetch_conditions` → `score_conditions` → `generate_message` → `send_notification`

**Current focus: `fetch_conditions` only.** Do not build the other stages yet.

Everything runs locally as a plain Python package first. AWS (Lambda, Step Functions,
DynamoDB) comes later, so keep the code free of AWS dependencies for now.

## Tech and conventions
- Python 3.12, managed with `uv`
- HTTP client: `httpx` with explicit timeouts (no `requests`)
- Data models: `dataclasses` or `pydantic`, fully type-hinted
- Lint/format: `ruff`; type check: `mypy --strict` on `src/`
- Tests: `pytest`, with mocked HTTP (`respx`) — tests never hit the network
- Config lives in `spots.yaml`; no hard-coded locations or thresholds
- Do not add new dependencies without asking me first

## Layout
```
src/gooddays/
  models.py        # Spot, HourlyConditions, Alert, SpotConditions
  storage.py       # Storage protocol + LocalStorage (reads spots.yaml)
  fetch/
    open_meteo.py  # forecast client
    nws.py         # alerts client
    service.py     # fetch_conditions(): orchestrates per spot
  cli.py           # `python -m gooddays fetch`
tests/
  fixtures/        # saved raw API responses (JSON)
spots.yaml
```

## What fetch_conditions does
**Input:** the list of spots from `Storage.get_spots()`.

**For each spot:**
1. Call Open-Meteo for a 2-day forecast:
   - hourly: `temperature_2m`, `precipitation_probability`, `precipitation`,
     `wind_speed_10m`, `wind_gusts_10m`, `snowfall`, `weather_code`
   - daily: `sunrise`, `sunset`
   - units: `temperature_unit=fahrenheit`, `wind_speed_unit=mph`,
     `precipitation_unit=inch`, `timezone=America/New_York`
2. Call the NWS API for active alerts at the spot's lat/lon
   (`/alerts/active?point={lat},{lon}`). NWS requires a descriptive
   `User-Agent` header; read it from config.
3. Normalize both responses into one `SpotConditions` object:
   hourly rows, daylight window, and a list of alerts (event, severity, headline, ends).

**Output:** `FetchResult` with `results: list[SpotConditions]` and
`errors: list[SpotError]`.

## Rules
- **No scoring or judgment here.** This stage only fetches and normalizes.
  "Is it good?" belongs in `score_conditions`.
- **One bad spot never fails the run.** Catch per-spot errors, record them in
  `errors`, and keep going.
- Retries: up to 3 attempts with exponential backoff on timeouts and 5xx only.
  Never retry 4xx.
- Timeouts: 10s per request.
- Fetch spots concurrently (async `httpx`), but cap concurrency at 5.
- Keep API clients thin: they return parsed raw responses. Normalization
  happens in `service.py`, so it can be tested from fixtures alone.
- Optional `--save-raw` flag writes raw responses to `data/raw/{date}/{spot_id}/`
  so I can replay them later as fixtures.
- Log with the standard `logging` module (structured key=value style), not `print`.

## Testing expectations
- Unit tests for normalization using saved fixtures (sunny day, rainy day,
  snow day, active winter storm alert, missing fields).
- Tests for error handling: timeout then success (retry works), 500 three
  times (spot lands in `errors`), 404 (no retry).
- A test that one failing spot doesn't stop the others.

## Commands
- Install: `uv sync`
- Run: `uv run python -m gooddays fetch [--save-raw] [--fixtures]`
- Test: `uv run pytest`
- Lint/types: `uv run ruff check . && uv run mypy src`

## Git workflow
- `main` is the stable branch; `develop` is the integration branch.
- Every feature gets its own branch off `develop` (e.g. `feature/<name>`).
  Push only to the feature branch, never directly to `develop` or `main`.
- Pull requests go from the feature branch into `develop`, not `main`.

## How to work with me
- Work in small steps and stop after each for me to review the diff.
- Explain any design decision that isn't obvious; I need to be able to
  defend every choice in an interview.
- Write or update tests alongside each change, and run them before saying done.