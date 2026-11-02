# CLAUDE.md — gooddays

## Project context
"Is it a good day?" is a personal app that checks weather conditions for my favorite
outdoor spots (hiking, biking, downhill and cross-country skiing) and alerts me when a day looks good.

The pipeline has four stages:
`fetch_conditions` → `score_conditions` → `generate_message` → `send_notification`

**`fetch_conditions` is built. Current focus: `score_conditions`.** Do not build
`generate_message` or `send_notification` yet.

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
  models.py        # Spot, HourlyConditions, Alert, SpotConditions, Thresholds, ScoreResult
  storage.py       # Storage protocol + LocalStorage (reads spots.yaml)
  score.py         # score_conditions(): pass/fail per spot, activity and day
  fetch/
    open_meteo.py  # forecast client
    nws.py         # alerts client
    service.py     # fetch_conditions(): orchestrates per spot
    raw.py         # --save-raw snapshots and --fixtures replay fetchers
  report.py        # text and JSON rendering of fetch and score results
  cli.py           # `python -m gooddays fetch` and `python -m gooddays score`
tests/
  fixtures/        # saved raw API responses (JSON)
spots.yaml
```

## What fetch_conditions does
**Input:** the list of spots from `Storage.get_spots()`.

**For each spot:**
1. Call Open-Meteo for a 2-day forecast:
   - hourly: `temperature_2m`, `precipitation_probability`, `precipitation`,
     `wind_speed_10m`, `wind_gusts_10m`, `snowfall`, `snow_depth`, `rain`, `weather_code`
   - daily: `sunrise`, `sunset`
   - units: `temperature_unit=fahrenheit`, `wind_speed_unit=mph`,
     `precipitation_unit=inch`, `length_unit=imperial` (snow depth comes back in
     feet and is converted to inches), `timezone=America/New_York`
   - `timeformat=unixtime`: with iso8601, Open-Meteo labels every time at one fixed
     UTC offset for the whole response, which is an hour off after a DST change.
2. Call the NWS API for active alerts at the spot's lat/lon
   (`/alerts/active?point={lat},{lon}`). NWS requires a descriptive
   `User-Agent` header; read it from config.
3. Normalize both responses into one `SpotConditions` object:
   hourly rows, daylight window, and a list of alerts (event, severity, headline, ends).

**Output:** `FetchResult` with `results: list[SpotConditions]` and
`errors: list[SpotError]`.

## What score_conditions does
A pure function: fetched conditions + `thresholds` from `spots.yaml` -> a pass/fail
(`good` / `not_good`) per spot, per activity, for today and tomorrow, each with its
checks (value and limit) and reasons. Biking has no rules yet (`no_rules`). Results are
only printed; nothing is stored or sent.

- **Hourly checks** run over daylight hours (sunrise to sunset). A day passes when a run of
  at least `min_run_hours` consecutive hours passes every check.
  - Hiking: temperature >= min, rain probability <= max, rain amount <= max per hour.
  - Downhill skiing (`downhill_skiing`): temperature < max and wind < max, in daylight hours.
  - Cross-country skiing (`cross_country_skiing`): snow depth >= min, min <= temperature < max,
    wind < max, rain probability <= max, and liquid rain (`rain`) <= max per hour (0 means any
    rain fails), in daylight hours. Fresh snow overnight is NOT required, and snow depth is an
    hourly rule. The config requires min temperature to be below max temperature.
- **Downhill snow is required**: the *total* overnight snowfall > `min_snowfall_in`
  (overnight = from the previous sunset, or the start of the data for today, to sunrise;
  if the previous day's sunset isn't in the data the day is "no data") OR snow depth on
  the ground > `min_snow_depth_in`. Weather codes don't count. (Open-Meteo's depth is modeled
  natural snow, not a resort's reported base.)
- **Alerts**: unknown alerts (`alerts=None`) never pass. Severe and Extreme alerts that
  are still active fail every activity; other severities are listed as notes. An alert
  that ends at or before the window start is over. The alert window starts at sunrise,
  except downhill skiing, where it starts overnight.
- **Missing data** never passes: a missing value fails its check (a reading missing for
  every hour, e.g. in an older saved response, is reported as "no values for this reading"), a day with no daylight
  window or no hourly rows is "no data", and today only counts the hours from the fetch time.
- Compare times through UTC: same-zone datetimes compare by wall clock, wrong across DST.

- The old activity name `skiing` was renamed to `downhill_skiing`; `skiing` in config is a
  ConfigError that says so.

## Rules
- **No scoring or judgment in the fetch stage.** This stage only fetches and normalizes.
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
- Run: `uv run python -m gooddays fetch [--spots PATH] [--json] [--save-raw | --fixtures DIR]`
  (needs `GOODDAYS_NWS_USER_AGENT`; `--fixtures` replays a `data/raw/{date}/` folder)
  Exit codes: 0 ok (spot errors alone still exit 0), 1 every spot failed, 2 bad usage/config,
  3 `--save-raw` couldn't save everything, 70 unexpected error. `--save-raw` replaces that day's
  folder only when the run completes, so a saved folder is never a mix of two runs.
- Score: `uv run python -m gooddays score [--spots PATH] [--json] [--fixtures DIR]`
  (fetches, then scores; same exit codes as fetch, and a day that isn't good is output,
  not an error). Thresholds are in `spots.yaml` under `thresholds:` and are all required
  (`min_run_hours` is a whole number from 1 to 24).
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