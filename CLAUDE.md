# CLAUDE.md — gooddays

## Project context
"Is it a good day?" is a personal app that checks weather conditions for my favorite
outdoor spots (hiking, biking, downhill and cross-country skiing) and alerts me when a day looks good.

The pipeline has four stages:
`fetch_conditions` → `score_conditions` → `generate_message` → `send_notification`

**All four stages are built** (`fetch_conditions`, `score_conditions`, `generate_message`,
`send_notification`). Moving it to AWS is a later step the user will decide on.

Everything runs locally as a plain Python package first. AWS (Lambda, Step Functions,
DynamoDB) comes later, so keep the code free of AWS dependencies for now.

## Tech and conventions
- Python 3.12, managed with `uv`
- HTTP client: `httpx` with explicit timeouts (no `requests`)
- Claude API: the official `anthropic` SDK, used only in `ai_writer.py`, with the
  `ANTHROPIC_API_KEY` environment variable (never committed or logged). It brings its own
  HTTP library (`httpx2`), so `respx` cannot mock it: tests use a fake `MessageWriter`.
- Data models: `dataclasses` or `pydantic`, fully type-hinted
- Lint/format: `ruff`; type check: `mypy --strict` on `src/`
- Tests: `pytest`, with mocked HTTP (`respx`) — tests never hit the network
- Config lives in `spots.yaml`; no hard-coded locations or thresholds
- Do not add new dependencies without asking me first

## Layout
```
src/gooddays/
  models.py        # Spot, HourlyConditions, Alert, SpotConditions, Thresholds, ScoreResult,
                   # GoodEntry, MessageFacts, Message, SendResult
  storage.py       # Storage protocol + LocalStorage (reads spots.yaml)
  score.py         # score_conditions(): pass/fail per spot, activity and day
  message.py       # generate_message(): one SMS digest of the good days (pure parts)
  notify.py        # send_notification(): recipient checks, narrow retry (pure parts)
  sms_sender.py    # the Twilio SmsSender: one httpx POST per message
  ai_writer.py     # the Claude writer behind generate_message (the only SDK user)
  fetch/
    open_meteo.py  # forecast client
    nws.py         # alerts client
    service.py     # fetch_conditions(): orchestrates per spot
    raw.py         # --save-raw snapshots and --fixtures replay fetchers
  report.py        # text and JSON rendering of fetch and score results
  cli.py           # `python -m gooddays fetch`, `score` and `message`
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

## What generate_message does
Turns the good days into ONE short SMS text (plain ASCII, body only, no subject), or into
nothing when there is nothing worth sending. Only printed for now; `send_notification`
(recipient, sending) is the next stage and is built.

- **Code decides what is mentioned**: only `good` entries for today and tomorrow, ordered by
  day, then spots in config order, then each spot's own activity order. Never the AI. Spots
  with unknown alerts are never good. No good days means no message and no AI call
  (`message` prints `nothing to send`).
- **Each entry** shows spot, activity, weekday and date with today/tomorrow, temperature
  range, maximum wind, daylight window, snow depth (skiing activities only, and only when it
  rounds to at least 1 in) and any minor alerts. Temperature and wind use the hours the
  scorer judged (daylight, minus hours already over); snow depth uses the whole daylight
  window, like the scorer. A short footer counts spots with any fetch error ("2 spots
  couldn't be checked"); code appends it. Names are cleaned to plain SMS text (ASCII only,
  none of `^{}\[]~|` or the backtick); a name with nothing left uses the spot id.
- **Claude writes the wording** from those facts (`claude-opus-5-5`, effort low, 30s
  timeout, the SDK's 2 retries, so a bad day can take up to about 90s before the fallback;
  the server-side refusal fallback is on). The facts go in as data; alert text is never
  treated as instructions.
- **Validation** (`validate_text`): plain SMS-safe ASCII, at most 306 characters (two
  multi-part SMS segments of 153; the footer counts), every entry named in its own
  "Today"/"Tomorrow" group with its activity (so a dropped entry is caught even when the spot
  appears elsewhere), no unknown spot, and no number that is not in the facts (a minus sign
  counts). Known limits: a real number attached to the wrong fact passes (a wind speed that is
  really a temperature), a place name that is not in the config is not caught, and alert
  notes are optional in the text.
- **Fallback**: any AI failure (no key, network, rate limit, refusal, truncated, empty or
  non-text reply, an unexpected exception) or failed validation uses a code-built text, and
  logs `message_fallback reason=...` at WARNING (INFO for `--no-ai`; an unexpected exception
  includes its traceback). Reasons never contain the key, the prompt, or names or numbers from
  the facts. If there are too many entries to list even in the shortest form, the AI is not
  called at all. A too-long fallback drops alert notes, then daylight, then snow depth, then
  entries ("+N more", always keeping at least one entry and the footer). Never split into
  several texts. A cap below 80 characters is refused.
- Moving to Amazon Bedrock later means changing `build_client`, `MODEL` and the fallback
  setting in `ai_writer.py` (the SDK has a Bedrock client); nothing else.

## What send_notification does
`python -m gooddays send` runs the whole pipeline and texts the result to one phone. It is
the only command that sends anything; `message` stays print-only.

- **Provider**: Twilio's REST API, called with `httpx` behind a replaceable `SmsSender`
  (`(recipient, body) -> message id`, raising `SendError(reason, retryable)`). A different
  vendor means one new adapter like `sms_sender.py`. No new dependency, and still no AWS.
  AT&T (the phone's carrier) is not the provider: its email-to-text gateway was reported
  shut down in June 2025.
- **Settings** (environment variables only; never in `spots.yaml`, never logged in full):
  `GOODDAYS_SMS_TO` (your number, international format like `+14155550123`),
  `GOODDAYS_SMS_FROM` (the Twilio number), `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`.
- **Safe by default**: nothing to send means no phone number or credentials are read and
  nothing is sent. `--dry-run` prints the text instead (allowed in CI). A real send is
  refused when `CI` is set to anything. A missing or invalid setting is a config error
  (exit 2) that names the setting, never its value. The phone number is masked
  (`+***0123`) everywhere and the body is not logged.
- **Sent means accepted**: Twilio returned a message id (`queued`), not that the text reached
  the phone. US carriers filter unregistered business texts, so a vendor number usually
  needs registration before texts reach an AT&T phone; trial accounts often send only to
  verified numbers. Verify these with Twilio.
- **Retries**: at most 2 attempts, only for connection failures, connect timeouts and 5xx;
  never for read timeouts or any 4xx, because a text is not idempotent. Exit 4 after a read
  timeout means "unknown", and the output says so.
- **Failure**: exit 4, the text is still printed, and a short reason key (`server_error`,
  `read_timeout`, `auth_rejected`, `rejected_21211`, ...). Twilio's error text is never kept
  (it can echo the number); a numeric code is only a hint in the key.
- **Repeats**: nothing is stored, so running it again texts the same message again; a
  warning says so. Dedup is deferred (a later design should key on day, spot and activity,
  not on the text, because Claude's wording changes).
- **Cost**: the segment count is logged (160 characters, or 153 per part once split), not a
  price.
- **Tests** can never send: an autouse fixture makes the real client impossible and clears
  `CI` and the settings above; they fake the sender, and the Twilio adapter itself is tested
  with `respx`.

## Rules
- **No scoring or judgment in the fetch stage.** This stage only fetches and normalizes.
  "Is it good?" belongs in `score_conditions`.
- **One bad spot never fails the run.** Catch per-spot errors, record them in
  `errors`, and keep going.
- Retries: up to 3 attempts with exponential backoff on timeouts and 5xx only.
  Never retry 4xx. (Two exceptions: the Claude SDK call in `ai_writer.py` uses the SDK's own retries,
  which also cover 408, 409 and 429; and the SMS POST in `notify.py` retries only connection
  failures and 5xx, at most twice in total.)
- Timeouts: 10s per request. (Except the Claude call, which gets 30s because Opus thinks first, and the SMS POST,
  which gets 10s and its own narrow retry rule, see "What send_notification does".)
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
- Message: `uv run python -m gooddays message [--spots PATH] [--fixtures DIR] [--no-ai]`
  (fetches, scores, then prints one SMS text or `nothing to send`; needs `ANTHROPIC_API_KEY`
  unless `--no-ai`, and falls back to the code-built text if Claude can't be used; exit codes
  as for score).
- Send: `uv run python -m gooddays send [--spots PATH] [--fixtures DIR] [--no-ai] [--dry-run]`
  (a REAL, billable text unless `--dry-run`; needs the four settings above. Exit codes as for
  score, plus 4 when the text could not be sent; the text is still printed).
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