# outdoor-alerts

"Is it a good day?" is a personal app that checks the weather at my favorite outdoor
spots (hiking, biking, downhill and cross-country skiing) and tells me when today or
tomorrow looks good. The Python package is called `gooddays`.

## How it works

A pipeline of stages, each runnable locally from the CLI:

```
fetch_conditions → score_conditions → generate_message → send_notification
```

| Stage | Status | What it does |
| --- | --- | --- |
| `fetch_conditions` | done | Calls Open-Meteo (2-day hourly forecast, sunrise/sunset) and the NWS API (active alerts) for each spot, and normalizes them into one `SpotConditions` per spot. |
| `score_conditions` | done | Applies the thresholds in `spots.yaml` and says pass/fail per spot, activity and day. |
| `generate_message` | done | Writes one short SMS digest of the good days. Claude writes the text; if it fails or its text fails checks, a plain text built by code is used instead. |
| `send_notification` | done | Emails the message through an AWS SNS topic (`send`). Nothing is sent unless you run `send` without `--dry-run`. |

Everything runs locally as a plain Python package. The only AWS code is the SNS
publisher used by `send` (`boto3`); Lambda, Step Functions and DynamoDB are planned for
later.

Fetching is resilient: spots are fetched concurrently (max 5 at a time), requests time
out after 10s, timeouts and 5xx responses are retried up to 3 times with exponential
backoff (4xx are never retried), and one failing spot is recorded as an error without
stopping the others.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
```

### Configuration

All spots and thresholds live in [spots.yaml](spots.yaml); nothing is hard-coded.

- **`settings`**: the timezone passed to Open-Meteo (e.g. `America/New_York`).
- **`thresholds`**: per-activity limits (temperature, wind, precipitation, snow, and
  `min_run_hours` of consecutive passing daylight hours).
- **`spots`**: each has a unique `id`, `name`, `lat`, `lon` and a list of `activities`
  (`hiking`, `biking`, `downhill_skiing`, `cross_country_skiing`).

NWS requires a descriptive `User-Agent` with contact info. Set it in your shell rather
than committing it:

```sh
export GOODDAYS_NWS_USER_AGENT="gooddays (you@example.com)"
```

The `message` and `send` commands use Claude and need `ANTHROPIC_API_KEY`. Use `--no-ai`
to skip it.

To send the email, `send` publishes to an AWS SNS topic. Set up once, outside the code:

1. Create a (standard) SNS topic, e.g. `good-days`.
2. Subscribe your email address to it and click the confirmation link AWS emails you.
   SNS emails only *confirmed* subscribers and still reports success otherwise.
3. Give your AWS user or role `sns:Publish` on the topic.

Then set these in your shell (never commit them):

```sh
export GOODDAYS_SNS_TOPIC_ARN="arn:aws:sns:us-east-1:123456789012:good-days"
export AWS_PROFILE="default"     # or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
```

The region is taken from the topic ARN. "Sent" means SNS accepted the message, not that an
email arrived. The email comes from AWS, as plain text with an unsubscribe footer.

## Usage

```sh
uv run python -m gooddays fetch   [--save-raw | --fixtures DIR] [--raw-root DIR] [--json] [--spots FILE]
uv run python -m gooddays score   [--fixtures DIR] [--json] [--spots FILE]
uv run python -m gooddays message [--fixtures DIR] [--no-ai] [--spots FILE]
uv run python -m gooddays send    [--fixtures DIR] [--no-ai] [--dry-run] [--spots FILE]
```

- `fetch` prints the normalized conditions for every spot.
- `score` fetches, then prints pass/fail per spot, activity and day (today and tomorrow).
- `message` fetches, scores and prints one SMS digest of the good days, or
  "nothing to send".
- `send` does what `message` does, then emails the result through SNS. It is a real
  publish unless you pass `--dry-run`; it refuses to send when the `CI` environment
  variable is set. Nothing is stored, so running it again sends the same email again.
- `--save-raw` (fetch only) also writes raw API responses to
  `data/raw/<date>/<spot_id>/` (git-ignored).
- `--fixtures DIR` replays a folder written by `--save-raw` instead of calling the APIs,
  so you can work offline, e.g. `--fixtures data/raw/2026-01-15`.
- `--json` prints JSON instead of text (logs go to stderr, so stdout stays clean).

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | OK (errors on individual spots alone still exit 0) |
| 1 | Every spot failed |
| 2 | Bad usage or config |
| 3 | `--save-raw` could not save everything |
| 4 | `send` could not get SNS to accept the text (the text is still printed) |
| 70 | Unexpected error |
| 130 | Interrupted (Ctrl-C) |
| 141 | stdout closed early (e.g. `| head`) |

## Project layout

```
src/gooddays/
  models.py        # Spot, HourlyConditions, Alert, SpotConditions, results
  storage.py       # Storage protocol + LocalStorage (reads spots.yaml)
  fetch/
    open_meteo.py  # forecast client
    nws.py         # alerts client
    http.py        # shared retry/timeout helper
    raw.py         # --save-raw / --fixtures support
    service.py     # fetch_conditions(): orchestrates per spot
  score.py         # score_conditions()
  message.py       # generate_message(): checks and code-built fallback text
  ai_writer.py     # Claude-backed message writer (only module using the Anthropic SDK)
  notify.py        # send_notification(): topic checks and the narrow retry
  sns_sender.py    # the AWS SNS publisher (the only boto3 user)
  report.py        # text and JSON output
  cli.py           # python -m gooddays ...
tests/
  fixtures/        # saved raw API responses (JSON)
spots.yaml
```

## Development

```sh
uv run pytest                                  # tests; HTTP is mocked with respx, no network
uv run ruff check . && uv run ruff format --check .
uv run mypy src                                # strict
```

CI runs the same lint, format, type and test steps on pushes and pull requests to
`main` and `develop`.

Git workflow: `main` is stable and `develop` is the integration branch. Each feature
gets its own `feature/<name>` branch off `develop`, and pull requests go from the
feature branch into `develop`.
