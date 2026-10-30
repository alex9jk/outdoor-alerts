import dataclasses
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx

from gooddays.fetch.nws import ALERTS_URL
from gooddays.fetch.open_meteo import FORECAST_URL
from gooddays.fetch.raw import (
    FILENAMES,
    META_FILENAME,
    RawSnapshot,
    read_fetch_time,
    replay_fetchers,
    spot_file,
)
from gooddays.fetch.service import fetch_conditions
from gooddays.models import Settings, Spot, SpotConditions
from helpers import load_fixture

SETTINGS = Settings(
    nws_user_agent="gooddays-test (t@example.com)", timezone="America/New_York"
)


def make_spot(n: int) -> Spot:
    return Spot(
        id=f"spot-{n}",
        name=f"Spot {n}",
        lat=40.0 + n,
        lon=-75.0,
        activities=("hiking",),
    )


def save(directory: Path, spot_id: str, filename: str, fixture_name: str) -> None:
    path = directory / spot_id / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(load_fixture(fixture_name)))


FETCHED = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)


def snapshot_in(tmp_path: Path, day: str = "2026-01-15") -> RawSnapshot:
    snapshot = RawSnapshot(tmp_path / day, FETCHED)
    snapshot.begin()
    return snapshot


def test_commit_creates_the_folder_with_files_and_fetch_time(tmp_path: Path) -> None:
    snapshot = snapshot_in(tmp_path)
    write = snapshot.writer()
    write("spot-0", "open_meteo", {"a": 1})
    write("spot-0", "nws", {"b": 2})

    snapshot.commit()

    final = tmp_path / "2026-01-15"
    assert json.loads((final / "spot-0" / "open_meteo.json").read_text()) == {"a": 1}
    assert json.loads((final / "spot-0" / "nws.json").read_text()) == {"b": 2}
    assert read_fetch_time(final) == FETCHED
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-01-15"]  # no leftovers


def test_nothing_appears_before_commit(tmp_path: Path) -> None:
    snapshot = snapshot_in(tmp_path)
    snapshot.writer()("spot-0", "nws", {"b": 2})

    assert not (tmp_path / "2026-01-15").exists()


def test_rerun_replaces_the_whole_earlier_folder(tmp_path: Path) -> None:
    first = snapshot_in(tmp_path)
    write = first.writer()
    write("spot-0", "open_meteo", {"run": 1})
    write("spot-0", "nws", {"run": 1})  # this one will be missing from run 2
    write("removed-spot", "nws", {"run": 1})
    first.commit()

    second = snapshot_in(tmp_path)
    second.writer()("spot-0", "open_meteo", {"run": 2})
    second.commit()

    final = tmp_path / "2026-01-15"
    assert json.loads((final / "spot-0" / "open_meteo.json").read_text()) == {"run": 2}
    assert not (final / "spot-0" / "nws.json").exists()  # no mix of two runs
    assert not (final / "removed-spot").exists()


def test_discard_keeps_the_earlier_folder(tmp_path: Path) -> None:
    first = snapshot_in(tmp_path)
    first.writer()("spot-0", "nws", {"run": 1})
    first.commit()

    second = snapshot_in(tmp_path)
    second.writer()("spot-0", "nws", {"run": 2})
    second.discard()

    assert json.loads(
        (tmp_path / "2026-01-15" / "spot-0" / "nws.json").read_text()
    ) == {"run": 1}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-01-15"]


def test_failed_swap_puts_the_earlier_folder_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = snapshot_in(tmp_path)
    first.writer()("spot-0", "nws", {"run": 1})
    first.commit()
    second = snapshot_in(tmp_path)
    second.writer()("spot-0", "nws", {"run": 2})

    real_rename = Path.rename
    calls = 0

    def flaky_rename(self: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:  # the staging -> final step
            raise OSError("disk went away")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with pytest.raises(OSError, match="disk went away"):
        second.commit()

    assert json.loads(
        (tmp_path / "2026-01-15" / "spot-0" / "nws.json").read_text()
    ) == {"run": 1}


def test_a_failed_write_is_counted_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    snapshot = snapshot_in(tmp_path)
    (tmp_path / ".2026-01-15.tmp" / "spot-0").write_text("a file, not a folder")

    with caplog.at_level(logging.WARNING):
        snapshot.writer()("spot-0", "nws", {"b": 2})

    assert snapshot.failures == 1
    assert "raw_save_failed spot=spot-0 source=nws" in caplog.text


def test_begin_fails_loudly_when_the_folder_cannot_be_made(tmp_path: Path) -> None:
    blocker = tmp_path / "raw"
    blocker.write_text("a file where the folder should go")

    with pytest.raises(OSError):
        RawSnapshot(blocker / "2026-01-15", FETCHED).begin()


@pytest.mark.parametrize("spot_id", ["../escape", "a/b", "Spot", "a b", ""])
def test_unsafe_spot_ids_are_refused(tmp_path: Path, spot_id: str) -> None:
    with pytest.raises(ValueError, match="unsafe spot id"):
        spot_file(tmp_path, spot_id, "nws")

    snapshot = snapshot_in(tmp_path)
    snapshot.writer()(spot_id, "nws", {})
    assert snapshot.failures == 1
    assert list(tmp_path.glob("**/*.json")) == []  # nothing written anywhere


async def test_replay_refuses_an_unsafe_spot_id(tmp_path: Path) -> None:
    unsafe = Spot(id="../x", name="X", lat=1.0, lon=1.0, activities=("hiking",))

    result = await fetch_conditions(
        [unsafe], SETTINGS, fetchers=replay_fetchers(tmp_path)
    )

    assert result.results == []
    assert all("unsafe spot id" in e.message for e in result.errors)


def test_read_fetch_time(tmp_path: Path) -> None:
    assert read_fetch_time(tmp_path) is None  # no meta.json (an older folder)

    (tmp_path / META_FILENAME).write_text("{not json")
    assert read_fetch_time(tmp_path) is None

    (tmp_path / META_FILENAME).write_text('{"fetched_at": "2026-01-15T09:30:00"}')
    assert read_fetch_time(tmp_path) is None  # no offset: can't trust it

    (tmp_path / META_FILENAME).write_text('{"fetched_at": "2026-01-15T09:30:00+00:00"}')
    assert read_fetch_time(tmp_path) == FETCHED


async def test_replay_goes_through_normal_normalization(tmp_path: Path) -> None:
    save(tmp_path, "spot-0", FILENAMES["open_meteo"], "open_meteo_rainy.json")
    save(tmp_path, "spot-0", FILENAMES["nws"], "nws_alerts_winter_storm.json")

    # No respx mock is active, so any real HTTP call would fail the test.
    result = await fetch_conditions(
        [make_spot(0)], SETTINGS, fetchers=replay_fetchers(tmp_path)
    )

    assert result.errors == []
    (conditions,) = result.results
    assert conditions.hourly[2].precipitation_in == 0.35
    assert conditions.alerts is not None
    assert conditions.alerts[0].event == "Winter Storm Warning"


async def test_replay_missing_alerts_file_keeps_weather(tmp_path: Path) -> None:
    save(tmp_path, "spot-0", FILENAMES["open_meteo"], "open_meteo_sunny.json")

    result = await fetch_conditions(
        [make_spot(0)], SETTINGS, fetchers=replay_fetchers(tmp_path)
    )

    (conditions,) = result.results
    assert conditions.alerts is None
    (error,) = result.errors
    assert (error.spot_id, error.source) == ("spot-0", "nws")
    assert error.message.startswith("no saved response at")


async def test_replay_missing_forecast_or_spot_drops_only_that_spot(
    tmp_path: Path,
) -> None:
    save(tmp_path, "spot-0", FILENAMES["open_meteo"], "open_meteo_sunny.json")
    save(tmp_path, "spot-0", FILENAMES["nws"], "nws_alerts_none.json")
    # spot-1 has only alerts; spot-2 has no folder at all.
    save(tmp_path, "spot-1", FILENAMES["nws"], "nws_alerts_none.json")

    result = await fetch_conditions(
        [make_spot(0), make_spot(1), make_spot(2)],
        SETTINGS,
        fetchers=replay_fetchers(tmp_path),
    )

    assert [r.spot.id for r in result.results] == ["spot-0"]
    problems = sorted((e.spot_id, e.source) for e in result.errors)
    assert problems == [
        ("spot-1", "open_meteo"),
        ("spot-2", "nws"),
        ("spot-2", "open_meteo"),
    ]


async def test_replay_bad_files_are_recorded(tmp_path: Path) -> None:
    (tmp_path / "spot-0").mkdir()
    (tmp_path / "spot-0" / FILENAMES["open_meteo"]).write_text("{not json")
    (tmp_path / "spot-0" / FILENAMES["nws"]).write_text("[1, 2]")

    result = await fetch_conditions(
        [make_spot(0)], SETTINGS, fetchers=replay_fetchers(tmp_path)
    )

    assert result.results == []
    messages = {e.source: e.message for e in result.errors}
    assert "not valid JSON" in messages["open_meteo"]
    assert "not a JSON object" in messages["nws"]


@respx.mock
async def test_saved_responses_replay_to_the_same_result(tmp_path: Path) -> None:
    respx.get(FORECAST_URL).respond(json=load_fixture("open_meteo_dst_fall_back.json"))
    respx.get(ALERTS_URL).respond(json=load_fixture("nws_alerts_winter_storm.json"))
    spots = [make_spot(0), make_spot(1)]

    snapshot = snapshot_in(tmp_path)
    live = await fetch_conditions(spots, SETTINGS, on_raw=snapshot.writer())
    snapshot.commit()
    replayed = await fetch_conditions(
        spots, SETTINGS, fetchers=replay_fetchers(snapshot.final)
    )

    def comparable(result_list: list[SpotConditions]) -> list[SpotConditions]:
        # fetched_at is when we looked, so it legitimately differs.
        return [dataclasses.replace(c, fetched_at=datetime.min) for c in result_list]

    assert live.errors == replayed.errors == []
    assert comparable(live.results) == comparable(replayed.results)
