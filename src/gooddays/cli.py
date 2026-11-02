"""Command line entry point: `fetch` and `score` for `python -m gooddays`."""

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gooddays.fetch.raw import RawSnapshot, read_fetch_time, replay_fetchers
from gooddays.fetch.service import Fetchers, fetch_conditions
from gooddays.models import Settings, Spot
from gooddays.report import score_to_json, score_to_text, to_json, to_text
from gooddays.score import score_conditions
from gooddays.storage import ConfigError, LocalStorage

logger = logging.getLogger(__name__)

DEFAULT_RAW_ROOT = Path("data/raw")

# Exit codes. 1 is reserved for "every spot failed", so crashes use other codes.
EXIT_OK = 0
EXIT_ALL_SPOTS_FAILED = 1
EXIT_USAGE_OR_CONFIG = 2
EXIT_SAVE_FAILED = 3  # --save-raw could not save everything
EXIT_UNEXPECTED = 70  # a bug or environment problem (sysexits EX_SOFTWARE)
EXIT_BROKEN_PIPE = 141  # stdout closed early, e.g. `| head` (128 + SIGPIPE)
EXIT_INTERRUPTED = 130  # Ctrl-C (128 + SIGINT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gooddays")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser(
        "fetch",
        help="fetch conditions for every spot",
        description=(
            "Exit codes: 0 ok (spot errors alone still exit 0), 1 every spot failed, "
            "2 bad usage or config, 3 --save-raw could not save everything, "
            "70 unexpected error, 130 interrupted, 141 stdout closed early."
        ),
    )
    _add_common_options(fetch)
    fetch.add_argument(
        "--raw-root",
        type=Path,
        default=DEFAULT_RAW_ROOT,
        help=f"where --save-raw writes <date>/ folders (default: {DEFAULT_RAW_ROOT})",
    )
    source = fetch.add_mutually_exclusive_group()
    source.add_argument(
        "--save-raw",
        action="store_true",
        help="also save raw API responses to <raw-root>/<today>/<spot_id>/, "
        "replacing that day's earlier folder if the run completes",
    )
    source.add_argument(
        "--fixtures",
        type=Path,
        metavar="DIR",
        help="replay a folder written by --save-raw "
        f"(e.g. {DEFAULT_RAW_ROOT}/2026-01-15) instead of calling the APIs",
    )

    score = commands.add_parser(
        "score",
        help="fetch conditions, then say whether each day is good per activity",
        description=(
            "Prints pass/fail per spot, activity, and day (today and tomorrow), "
            "using the thresholds in the config file. Same exit codes as fetch "
            "(a day that isn't good is output, not an error), except 3 never applies."
        ),
    )
    _add_common_options(score)
    score.add_argument(
        "--fixtures",
        type=Path,
        metavar="DIR",
        help="score a folder written by `fetch --save-raw` instead of calling the APIs",
    )
    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--spots",
        type=Path,
        default=Path("spots.yaml"),
        help="config file (default: spots.yaml in the current directory)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON, not text")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _run(argv)
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_BROKEN_PIPE
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception:
        logger.exception("unexpected_error")
        print("error: unexpected failure (details logged above)", file=sys.stderr)
        return EXIT_UNEXPECTED


def _run(argv: Sequence[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,  # keeps stdout clean for --json
        format="time=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
    )
    # httpx logs every request URL at INFO; the service already logs what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.fixtures is not None and not args.fixtures.is_dir():
        parser.error(f"--fixtures: not a directory: {args.fixtures}")

    try:
        storage = LocalStorage(args.spots)
        settings = storage.get_settings()
        spots = storage.get_spots()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_USAGE_OR_CONFIG

    if args.command == "score":
        return _score(args, settings, spots, storage)
    return _fetch(args, settings, spots)


def _score(
    args: argparse.Namespace,
    settings: Settings,
    spots: list[Spot],
    storage: LocalStorage,
) -> int:
    try:
        thresholds = storage.get_thresholds()  # checked before anything is fetched
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_USAGE_OR_CONFIG
    fetchers, fetched_at = _replay(args.fixtures)
    result = asyncio.run(
        fetch_conditions(spots, settings, fetchers=fetchers, fetched_at=fetched_at)
    )
    scores = score_conditions(result.results, thresholds, settings.timezone)
    _emit(
        score_to_json(scores, result.errors)
        if args.json
        else score_to_text(scores, result.errors)
    )
    return EXIT_ALL_SPOTS_FAILED if result.errors and not result.results else EXIT_OK


def _fetch(args: argparse.Namespace, settings: Settings, spots: list[Spot]) -> int:
    started = datetime.now(ZoneInfo(settings.timezone))
    snapshot: RawSnapshot | None = None
    on_raw = None
    fetchers: Fetchers | None = None
    fetched_at: datetime | None = None
    if args.save_raw:
        snapshot = RawSnapshot(args.raw_root / started.date().isoformat(), started)
        try:
            snapshot.begin()
        except OSError as e:
            print(
                f"cannot save raw responses under {args.raw_root}: {e}", file=sys.stderr
            )
            return EXIT_SAVE_FAILED
        on_raw = snapshot.writer()
    else:
        fetchers, fetched_at = _replay(args.fixtures)

    try:
        result = asyncio.run(
            fetch_conditions(
                spots,
                settings,
                on_raw=on_raw,
                fetchers=fetchers,
                fetched_at=fetched_at,
            )
        )
        _emit(to_json(result) if args.json else to_text(result))
        saved = _finish_snapshot(snapshot, has_results=bool(result.results))
    finally:
        if snapshot is not None:
            snapshot.discard()  # a no-op once committed

    if result.errors and not result.results:
        return EXIT_ALL_SPOTS_FAILED
    return EXIT_OK if saved else EXIT_SAVE_FAILED


def _replay(directory: Path | None) -> tuple[Fetchers | None, datetime | None]:
    """Fetchers and fetch time for --fixtures, or (None, None) for a live run."""
    if directory is None:
        return None, None
    fetched_at = read_fetch_time(directory)
    if fetched_at is None:
        logger.warning(
            "replay_without_fetch_time dir=%r note=%r",
            str(directory),
            "results will show the replay time as fetched_at",
        )
    return replay_fetchers(directory), fetched_at


def _finish_snapshot(snapshot: RawSnapshot | None, *, has_results: bool) -> bool:
    """Commit a complete --save-raw run. Returns False if saving fell short."""
    if snapshot is None:
        return True
    if not has_results:
        print(
            "nothing saved: no spot succeeded (kept any earlier folder)",
            file=sys.stderr,
        )
        return True  # the run already fails with EXIT_ALL_SPOTS_FAILED
    if snapshot.failures:
        print(
            f"warning: {snapshot.failures} raw file(s) could not be saved, so "
            f"{snapshot.final} was not replaced (kept any earlier folder)",
            file=sys.stderr,
        )
        return False
    try:
        snapshot.commit()
    except OSError as e:
        print(f"warning: could not save to {snapshot.final}: {e}", file=sys.stderr)
        return False
    return True


def _emit(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _silence_stdout() -> None:
    """Stop Python complaining again about the closed pipe when it exits."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError):
        pass  # no real file descriptor (e.g. under test capture)
