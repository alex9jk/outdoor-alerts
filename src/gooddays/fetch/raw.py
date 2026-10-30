"""Save raw API responses to disk (--save-raw) and replay them (--fixtures).

Layout: {directory}/{spot_id}/open_meteo.json and {directory}/{spot_id}/nws.json.
Replaying goes through the same normalization and error handling as a live run.
Only successful responses are saved, so replay can't reproduce HTTP errors.
"""

import json
import logging
from pathlib import Path

import httpx

from gooddays.fetch.http import FetchError, RawCallback, Source
from gooddays.fetch.service import Fetcher, Fetchers
from gooddays.models import Settings, Spot

logger = logging.getLogger(__name__)

FILENAMES: dict[Source, str] = {"open_meteo": "open_meteo.json", "nws": "nws.json"}


def raw_writer(directory: Path) -> RawCallback:
    """A callback that writes each raw response under `directory`.

    Re-running into the same directory overwrites the earlier files.
    """

    def write(spot_id: str, source: Source, raw: dict[str, object]) -> None:
        path = directory / spot_id / FILENAMES[source]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2) + "\n")
        logger.info("raw_saved spot=%s source=%s path=%s", spot_id, source, path)

    return write


def replay_fetchers(directory: Path) -> Fetchers:
    """Fetchers that read the files written by `raw_writer` instead of calling APIs."""
    return Fetchers(
        forecast=_reader(directory, "open_meteo"), alerts=_reader(directory, "nws")
    )


def _reader(directory: Path, source: Source) -> Fetcher:
    # Small local files, so a plain blocking read is fine.
    async def read(
        client: httpx.AsyncClient, spot: Spot, settings: Settings
    ) -> dict[str, object]:
        path = directory / spot.id / FILENAMES[source]
        try:
            text = path.read_text()
        except FileNotFoundError:
            raise FetchError(source, f"no saved response at {path}") from None
        except OSError as e:
            raise FetchError(source, f"cannot read {path}: {e}") from e
        try:
            data = json.loads(text)
        except ValueError:
            raise FetchError(
                source, f"saved response is not valid JSON: {path}"
            ) from None
        if not isinstance(data, dict):
            raise FetchError(source, f"saved response is not a JSON object: {path}")
        return data

    return read
