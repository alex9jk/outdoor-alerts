"""Save raw API responses to disk (--save-raw) and replay them (--fixtures).

Layout of one saved run:
    {directory}/meta.json                  when the run was fetched
    {directory}/{spot_id}/open_meteo.json
    {directory}/{spot_id}/nws.json

Replaying goes through the same normalization and error handling as a live run.
Only successful responses are saved, so replay can't reproduce HTTP errors.
"""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

import httpx

from gooddays.fetch.http import FetchError, RawCallback, Source
from gooddays.fetch.service import Fetcher, Fetchers
from gooddays.models import SPOT_ID_PATTERN, Settings, Spot

logger = logging.getLogger(__name__)

FILENAMES: dict[Source, str] = {"open_meteo": "open_meteo.json", "nws": "nws.json"}
META_FILENAME = "meta.json"


def spot_file(directory: Path, spot_id: str, source: Source) -> Path:
    """Where one spot's raw response lives. Refuses ids that aren't safe slugs."""
    if not SPOT_ID_PATTERN.fullmatch(spot_id):
        raise ValueError(f"unsafe spot id: {spot_id!r}")
    return directory / spot_id / FILENAMES[source]


class RawSnapshot:
    """One --save-raw run, written so a re-run never leaves a mix of two runs.

    Files go to a staging folder next to `final`. Only `commit()` replaces
    `final` (the whole folder, so spots dropped from the config disappear too).
    If the run is incomplete, `discard()` leaves any earlier folder untouched.
    """

    def __init__(self, final: Path, fetched_at: datetime) -> None:
        self.final = final
        self.fetched_at = fetched_at
        self.failures = 0
        self._staging = final.with_name(f".{final.name}.tmp")
        self._old = final.with_name(f".{final.name}.old")

    def begin(self) -> None:
        """Prepare an empty staging folder. Raises OSError if it can't."""
        shutil.rmtree(self._staging, ignore_errors=True)
        self._staging.mkdir(parents=True)

    def writer(self) -> RawCallback:
        """A callback that writes each raw response into the staging folder.

        A failed write is counted in `failures` (and logged), never raised.
        """

        def write(spot_id: str, source: Source, raw: dict[str, object]) -> None:
            try:
                path = spot_file(self._staging, spot_id, source)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
            except (OSError, ValueError) as e:
                self.failures += 1
                logger.warning(
                    "raw_save_failed spot=%s source=%s error=%r",
                    spot_id,
                    source,
                    f"{type(e).__name__}: {e}",
                )
                return
            logger.info(
                "raw_saved spot=%s source=%s path=%r", spot_id, source, str(path)
            )

        return write

    def commit(self) -> None:
        """Replace `final` with the staging folder. Raises OSError on failure.

        If the swap fails halfway, the earlier folder is put back.
        """
        (self._staging / META_FILENAME).write_text(
            json.dumps({"fetched_at": self.fetched_at.isoformat()}) + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(self._old, ignore_errors=True)
        had_earlier = self.final.exists()
        if had_earlier:
            self.final.rename(self._old)
        try:
            self._staging.rename(self.final)
        except OSError:
            if had_earlier:
                self._old.rename(self.final)
            raise
        shutil.rmtree(self._old, ignore_errors=True)

    def discard(self) -> None:
        """Drop the staging folder, keeping whatever was saved before."""
        shutil.rmtree(self._staging, ignore_errors=True)


def read_fetch_time(directory: Path) -> datetime | None:
    """When a saved run was fetched, or None for older folders without a record."""
    try:
        data = json.loads((directory / META_FILENAME).read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(data["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return fetched if fetched.tzinfo is not None else None


def replay_fetchers(directory: Path) -> Fetchers:
    """Fetchers that read a saved run instead of calling the APIs."""
    return Fetchers(
        forecast=_reader(directory, "open_meteo"), alerts=_reader(directory, "nws")
    )


def _reader(directory: Path, source: Source) -> Fetcher:
    # Small local files, so a plain blocking read is fine.
    async def read(
        client: httpx.AsyncClient, spot: Spot, settings: Settings
    ) -> dict[str, object]:
        try:
            path = spot_file(directory, spot.id, source)
        except ValueError as e:
            raise FetchError(source, str(e)) from None
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise FetchError(source, f"no saved response at {path}") from None
        except (OSError, UnicodeDecodeError) as e:
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
