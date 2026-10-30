import dataclasses
import json
from datetime import datetime
from pathlib import Path

import respx

from gooddays.fetch.nws import ALERTS_URL
from gooddays.fetch.open_meteo import FORECAST_URL
from gooddays.fetch.raw import FILENAMES, raw_writer, replay_fetchers
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


def test_writer_saves_each_source_under_the_spot(tmp_path: Path) -> None:
    write = raw_writer(tmp_path / "2026-01-15")

    write("spot-0", "open_meteo", {"a": 1})
    write("spot-0", "nws", {"b": 2})

    base = tmp_path / "2026-01-15" / "spot-0"
    assert json.loads((base / "open_meteo.json").read_text()) == {"a": 1}
    assert json.loads((base / "nws.json").read_text()) == {"b": 2}


def test_writer_overwrites_the_same_day(tmp_path: Path) -> None:
    write = raw_writer(tmp_path)
    write("spot-0", "nws", {"v": 1})
    write("spot-0", "nws", {"v": 2})

    assert json.loads((tmp_path / "spot-0" / "nws.json").read_text()) == {"v": 2}


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

    live = await fetch_conditions(spots, SETTINGS, on_raw=raw_writer(tmp_path))
    replayed = await fetch_conditions(
        spots, SETTINGS, fetchers=replay_fetchers(tmp_path)
    )

    def comparable(result_list: list[SpotConditions]) -> list[SpotConditions]:
        # fetched_at is when we looked, so it legitimately differs.
        return [dataclasses.replace(c, fetched_at=datetime.min) for c in result_list]

    assert live.errors == replayed.errors == []
    assert comparable(live.results) == comparable(replayed.results)
