"""Command line entry point: `python -m gooddays fetch`."""

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
from gooddays.report import to_json, to_text
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
    fetch.add_argument(
        "--spots",
        type=Path,
        default=Path("spots.yaml"),
        help="config file (default: spots.yaml in the current directory)",
    )
    fetch.add_argument("--json", action="store_true", help="print JSON, not text")
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
    return parser


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
    elif args.fixtures is not None:
        fetchers = replay_fetchers(args.fixtures)
        fetched_at = read_fetch_time(args.fixtures)
        if fetched_at is None:
            logger.warning(
                "replay_without_fetch_time dir=%r note=%r",
                str(args.fixtures),
                "results will show the replay time as fetched_at",
            )

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
