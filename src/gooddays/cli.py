"""Command line entry point: `python -m gooddays fetch`."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gooddays.fetch.raw import raw_writer, replay_fetchers
from gooddays.fetch.service import Fetchers, fetch_conditions
from gooddays.report import to_json, to_text
from gooddays.storage import ConfigError, LocalStorage

RAW_ROOT = Path("data/raw")

EXIT_OK = 0
EXIT_FAILED = 1  # no spot produced a result
EXIT_USAGE_OR_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gooddays")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="fetch conditions for every spot")
    fetch.add_argument(
        "--spots",
        type=Path,
        default=Path("spots.yaml"),
        help="config file (default: spots.yaml in the current directory)",
    )
    fetch.add_argument("--json", action="store_true", help="print JSON, not text")
    source = fetch.add_mutually_exclusive_group()
    source.add_argument(
        "--save-raw",
        action="store_true",
        help=f"also save raw API responses to {RAW_ROOT}/<today>/<spot_id>/",
    )
    source.add_argument(
        "--fixtures",
        type=Path,
        metavar="DIR",
        help=f"replay a folder written by --save-raw (e.g. {RAW_ROOT}/2026-01-15) "
        "instead of calling the APIs",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,  # keeps stdout clean for --json
        format="time=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
    )

    if args.fixtures is not None and not args.fixtures.is_dir():
        parser.error(f"--fixtures: not a directory: {args.fixtures}")

    try:
        storage = LocalStorage(args.spots)
        settings = storage.get_settings()
        spots = storage.get_spots()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_USAGE_OR_CONFIG

    on_raw = None
    fetchers: Fetchers | None = None
    if args.save_raw:
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        on_raw = raw_writer(RAW_ROOT / today.isoformat())
    elif args.fixtures is not None:
        fetchers = replay_fetchers(args.fixtures)

    result = asyncio.run(
        fetch_conditions(spots, settings, on_raw=on_raw, fetchers=fetchers)
    )
    print(to_json(result) if args.json else to_text(result))
    return EXIT_FAILED if result.errors and not result.results else EXIT_OK
