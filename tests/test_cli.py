import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from gooddays import cli
from gooddays.cli import main
from gooddays.fetch.nws import ALERTS_URL
from gooddays.fetch.open_meteo import FORECAST_URL
from gooddays.message import MESSAGE_CAP, WriterError
from gooddays.notify import SendError
from gooddays.storage import USER_AGENT_ENV_VAR
from helpers import load_fixture

THRESHOLDS_YAML = """
thresholds:
  hiking:
    min_temperature_f: 45
    max_precipitation_probability_pct: 30
    max_precipitation_in: 0.1
    min_run_hours: 4
  downhill_skiing:
    max_temperature_f: 32
    max_wind_mph: 10
    min_snowfall_in: 0
    min_snow_depth_in: 0
    min_run_hours: 4
  cross_country_skiing:
    min_snow_depth_in: 5
    min_temperature_f: 10
    max_temperature_f: 32
    max_wind_mph: 10
    max_precipitation_probability_pct: 30
    max_rain_in: 0
    min_run_hours: 4
"""

SPOTS_YAML = (
    """
settings:
  timezone: America/New_York
spots:
  - {id: spot-0, name: Spot 0, lat: 40.0, lon: -75.0, activities: [hiking]}
  - {id: spot-1, name: Spot 1, lat: 41.0, lon: -75.0, activities: [downhill_skiing]}
"""
    + THRESHOLDS_YAML
)


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch working directory with a spots.yaml and a user agent set."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "gooddays-test (t@example.com)")
    (tmp_path / "spots.yaml").write_text(SPOTS_YAML)
    return tmp_path


def mock_ok() -> tuple[respx.Route, respx.Route]:
    return (
        respx.get(FORECAST_URL).respond(json=load_fixture("open_meteo_sunny.json")),
        respx.get(ALERTS_URL).respond(json=load_fixture("nws_alerts_none.json")),
    )


def lat_of(request: httpx.Request) -> float:
    if "latitude" in request.url.params:
        return float(request.url.params["latitude"])
    return float(request.url.params["point"].split(",")[0])


def today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def write_saved_run(
    folder: Path, spot_ids: tuple[str, ...] = ("spot-0", "spot-1")
) -> Path:
    """A saved-run folder like --save-raw writes (without meta.json)."""
    for spot_id in spot_ids:
        (folder / spot_id).mkdir(parents=True)
        (folder / spot_id / "open_meteo.json").write_text(
            json.dumps(load_fixture("open_meteo_sunny.json"))
        )
        (folder / spot_id / "nws.json").write_text(
            json.dumps(load_fixture("nws_alerts_none.json"))
        )
    return folder


# --- output ---


@respx.mock
def test_text_output(capsys: pytest.CaptureFixture[str]) -> None:
    mock_ok()

    assert main(["fetch"]) == 0

    out = capsys.readouterr().out
    assert "Spot 0 (spot-0)" in out
    assert "Spot 1 (spot-1)" in out
    assert "Alerts: none" in out
    assert out.rstrip().endswith("2 spot(s) ok, 0 error(s)")


@respx.mock
def test_json_output_is_only_json_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    mock_ok()

    assert main(["fetch", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert [r["spot"]["id"] for r in data["results"]] == ["spot-0", "spot-1"]
    assert data["errors"] == []


@respx.mock
def test_json_output_when_every_spot_fails(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(FORECAST_URL).respond(404)
    respx.get(ALERTS_URL).respond(404)

    assert main(["fetch", "--json"]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["results"] == []
    assert len(data["errors"]) == 4  # both APIs, both spots


def test_logs_go_to_stderr_and_stdout_stays_clean(project: Path) -> None:
    folder = write_saved_run(project / "saved")

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "gooddays",
            "fetch",
            "--fixtures",
            str(folder),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0
    assert len(json.loads(done.stdout)["results"]) == 2  # stdout is only the JSON
    assert "level=INFO logger=gooddays.fetch.service" in done.stderr
    assert "level=" not in done.stdout


@respx.mock
def test_httpx_request_logging_is_quieted() -> None:
    mock_ok()

    main(["fetch"])

    assert logging.getLogger("httpx").level == logging.WARNING


# --- exit codes ---


@respx.mock
def test_spot_errors_alone_do_not_fail_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forecast(request: httpx.Request) -> httpx.Response:
        if lat_of(request) == 41.0:
            return httpx.Response(500)
        return httpx.Response(200, json=load_fixture("open_meteo_sunny.json"))

    respx.get(FORECAST_URL).mock(side_effect=forecast)
    respx.get(ALERTS_URL).respond(json=load_fixture("nws_alerts_none.json"))

    assert main(["fetch"]) == 0

    out = capsys.readouterr().out
    assert "ERROR spot-1 [open_meteo] HTTP 500 (status 500)" in out
    assert out.rstrip().endswith("1 spot(s) ok, 1 error(s)")


@respx.mock
def test_every_spot_failing_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(FORECAST_URL).respond(404)
    respx.get(ALERTS_URL).respond(404)

    assert main(["fetch"]) == 1

    assert "0 spot(s) ok" in capsys.readouterr().out


def test_missing_spots_file_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch", "--spots", "nope.yaml"]) == 2

    assert "config error: cannot read nope.yaml" in capsys.readouterr().err


def test_spots_file_that_is_not_utf8_exits_2(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "spots.yaml").write_bytes(b"\xff\xfe spots: []")

    assert main(["fetch"]) == 2

    assert "not valid UTF-8" in capsys.readouterr().err


def test_missing_user_agent_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(USER_AGENT_ENV_VAR)

    assert main(["fetch"]) == 2

    assert USER_AGENT_ENV_VAR in capsys.readouterr().err


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])

    assert exc.value.code == 2


def test_unexpected_error_exits_70_not_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "fetch_conditions", explode)

    assert main(["fetch"]) == 70

    assert "unexpected failure" in capsys.readouterr().err


def test_closed_stdout_exits_141_quietly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def closed(text: str) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(cli, "_emit", closed)
    folder = write_saved_run(Path("saved"))

    assert main(["fetch", "--fixtures", str(folder)]) == 141

    assert "Traceback" not in capsys.readouterr().err


def test_ctrl_c_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(argv: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run", interrupted)

    assert main(["fetch"]) == 130


# --- --save-raw ---


@respx.mock
def test_save_raw_writes_todays_folder_with_the_fetch_time(project: Path) -> None:
    mock_ok()

    assert main(["fetch", "--save-raw"]) == 0

    base = project / "data" / "raw" / today_et()
    for spot_id in ("spot-0", "spot-1"):
        assert json.loads((base / spot_id / "open_meteo.json").read_text()) == (
            load_fixture("open_meteo_sunny.json")
        )
        assert json.loads((base / spot_id / "nws.json").read_text()) == (
            load_fixture("nws_alerts_none.json")
        )
    fetched = datetime.fromisoformat(
        json.loads((base / "meta.json").read_text())["fetched_at"]
    )
    assert fetched.utcoffset() is not None
    assert sorted(p.name for p in base.parent.iterdir()) == [today_et()]  # no leftovers


@respx.mock
def test_save_raw_replaces_the_earlier_folder_for_the_day(project: Path) -> None:
    mock_ok()
    earlier = project / "data" / "raw" / today_et()
    (earlier / "gone-spot").mkdir(parents=True)
    (earlier / "spot-0").mkdir()
    (earlier / "spot-0" / "stale.json").write_text("{}")

    assert main(["fetch", "--save-raw"]) == 0

    assert not (earlier / "gone-spot").exists()
    assert not (earlier / "spot-0" / "stale.json").exists()
    assert (earlier / "spot-0" / "open_meteo.json").exists()


@respx.mock
def test_save_raw_with_nothing_succeeding_keeps_the_earlier_folder(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(FORECAST_URL).respond(500)
    respx.get(ALERTS_URL).respond(500)
    earlier = project / "data" / "raw" / today_et()
    earlier.mkdir(parents=True)
    (earlier / "keep.json").write_text("{}")

    assert main(["fetch", "--save-raw"]) == 1

    assert (earlier / "keep.json").exists()
    assert "nothing saved" in capsys.readouterr().err
    assert sorted(p.name for p in earlier.parent.iterdir()) == [today_et()]


@respx.mock
def test_save_raw_write_failure_exits_3_and_keeps_the_earlier_folder(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_ok()
    earlier = project / "data" / "raw" / today_et()
    earlier.mkdir(parents=True)
    (earlier / "keep.json").write_text("{}")
    real = cli.RawSnapshot.writer

    def failing_writer(self: cli.RawSnapshot) -> Any:
        write = real(self)

        def flaky(spot_id: str, source: Any, raw: Any) -> None:
            if spot_id == "spot-1":
                self.failures += 1  # what a failed disk write records
                return
            write(spot_id, source, raw)

        return flaky

    monkeypatch.setattr(cli.RawSnapshot, "writer", failing_writer)

    assert main(["fetch", "--save-raw"]) == 3

    captured = capsys.readouterr()
    assert "2 spot(s) ok" in captured.out  # the report itself still printed
    assert "2 raw file(s) could not be saved" in captured.err
    assert (earlier / "keep.json").exists()
    assert not (earlier / "spot-0").exists()  # the incomplete run wasn't kept


@respx.mock
def test_save_raw_into_an_unwritable_root_exits_3_before_fetching(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    forecast_route, alerts_route = mock_ok()
    (project / "blocked").write_text("a file where the folder should go")

    assert (
        main(["fetch", "--save-raw", "--raw-root", str(project / "blocked" / "raw")])
        == 3
    )

    assert "cannot save raw responses" in capsys.readouterr().err
    assert forecast_route.call_count == alerts_route.call_count == 0


@respx.mock
def test_custom_raw_root(project: Path) -> None:
    mock_ok()

    assert main(["fetch", "--save-raw", "--raw-root", str(project / "elsewhere")]) == 0

    assert (project / "elsewhere" / today_et() / "spot-0" / "nws.json").exists()
    assert not (project / "data").exists()


# --- --fixtures ---


@respx.mock
def test_fixtures_replays_a_saved_folder_without_the_network(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    forecast_route, alerts_route = mock_ok()
    assert main(["fetch", "--save-raw"]) == 0
    live_out = capsys.readouterr().out
    folder = project / "data" / "raw" / today_et()

    assert main(["fetch", "--fixtures", str(folder)]) == 0

    replay_out = capsys.readouterr().out
    assert forecast_route.call_count == alerts_route.call_count == 2  # no new calls

    def without_fetch_times(text: str) -> list[str]:
        return [re.sub(r"fetched .*", "fetched", line) for line in text.splitlines()]

    assert without_fetch_times(replay_out) == without_fetch_times(live_out)


@respx.mock
def test_replay_reports_when_the_data_was_fetched_not_when_replayed(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_ok()
    assert main(["fetch", "--save-raw"]) == 0
    capsys.readouterr()
    folder = project / "data" / "raw" / today_et()
    meta = json.loads((folder / "meta.json").read_text())
    meta["fetched_at"] = "2020-01-02T03:04:05-05:00"
    (folder / "meta.json").write_text(json.dumps(meta))

    assert main(["fetch", "--fixtures", str(folder), "--json"]) == 0

    results = json.loads(capsys.readouterr().out)["results"]
    assert {r["fetched_at"] for r in results} == {"2020-01-02T03:04:05-05:00"}


def test_replay_of_a_folder_without_meta_still_works(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = write_saved_run(project / "saved")

    assert main(["fetch", "--fixtures", str(folder)]) == 0

    assert "2 spot(s) ok" in capsys.readouterr().out


def test_fixtures_with_missing_files_reports_errors(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project / "saved"
    (folder / "spot-0").mkdir(parents=True)
    (folder / "spot-0" / "open_meteo.json").write_text(
        json.dumps(load_fixture("open_meteo_sunny.json"))
    )

    assert main(["fetch", "--fixtures", str(folder)]) == 0  # spot-0 still worked

    out = capsys.readouterr().out
    assert "Alerts: unknown" in out
    assert "ERROR spot-0 [nws] no saved response at" in out
    assert "ERROR spot-1 [open_meteo] no saved response at" in out


def test_fixtures_folder_with_nothing_usable_exits_1(project: Path) -> None:
    (project / "empty").mkdir()

    assert main(["fetch", "--fixtures", str(project / "empty")]) == 1


def test_save_raw_and_fixtures_are_mutually_exclusive(project: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["fetch", "--save-raw", "--fixtures", str(project)])

    assert exc.value.code == 2


def test_fixtures_must_be_a_directory(project: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["fetch", "--fixtures", str(project / "nope")])

    assert exc.value.code == 2


def test_fixtures_pointing_at_a_file_is_refused(project: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["fetch", "--fixtures", str(project / "spots.yaml")])

    assert exc.value.code == 2


# --- other options ---


@respx.mock
def test_custom_spots_path(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mock_ok()
    other = project / "other.yaml"
    other.write_text(SPOTS_YAML.replace("spot-1", "spot-9").replace("Spot 1", "Spot 9"))

    assert main(["fetch", "--spots", str(other)]) == 0

    assert "Spot 9 (spot-9)" in capsys.readouterr().out


def test_runs_as_a_module() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "gooddays", "fetch", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0
    assert "--save-raw" in done.stdout
    assert "--fixtures" in done.stdout
    assert "Exit codes" in done.stdout


# --- score ---


def row(day: str, activity: str, status: str) -> str:
    return f"  {day}  {activity:<20} {status}"


def write_warm_dry_run(folder: Path) -> Path:
    """A saved run of the warm, dry June 15-16 fixture, fetched early on June 15."""
    write_saved_run(folder)
    for spot_id in ("spot-0", "spot-1"):
        (folder / spot_id / "open_meteo.json").write_text(
            json.dumps(load_fixture("open_meteo_warm_dry.json"))
        )
    (folder / "meta.json").write_text(
        json.dumps({"fetched_at": "2026-06-15T04:00:00-04:00"})
    )
    return folder


def test_score_from_a_saved_folder_without_the_network(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = write_warm_dry_run(project / "saved")

    assert main(["score", "--fixtures", str(folder)]) == 0

    out = capsys.readouterr().out
    assert "Spot 0 (spot-0)" in out
    assert row("2026-06-15", "hiking", "GOOD") in out
    assert row("2026-06-16", "hiking", "GOOD") in out
    assert row("2026-06-15", "downhill_skiing", "NOT GOOD") in out  # warm, no snow
    assert "snow: no snow overnight and none on the ground" in out
    assert out.rstrip().endswith("2 spot(s) scored, 2 good day(s), 0 error(s)")


def test_score_json_is_only_json_on_stdout(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = write_warm_dry_run(project / "saved")

    assert main(["score", "--fixtures", str(folder), "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert [s["spot"]["id"] for s in data["scores"]] == ["spot-0", "spot-1"]
    hiking = data["scores"][0]["scores"][0]
    assert (hiking["activity"], hiking["day"], hiking["status"]) == (
        "hiking",
        "2026-06-15",
        "good",
    )
    assert data["errors"] == []


def test_score_lists_fetch_errors_and_scores_the_rest(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = write_warm_dry_run(project / "saved")
    (folder / "spot-1" / "open_meteo.json").unlink()  # spot-1 can't be fetched

    assert main(["score", "--fixtures", str(folder)]) == 0

    out = capsys.readouterr().out
    assert "Spot 0 (spot-0)" in out
    assert "Spot 1 (spot-1)" not in out  # no forecast, no score
    assert "ERROR spot-1 [open_meteo] no saved response at" in out


def test_score_when_nothing_could_be_fetched_exits_1(project: Path) -> None:
    (project / "empty").mkdir()

    assert main(["score", "--fixtures", str(project / "empty")]) == 1


def test_score_alerts_unknown_means_not_good(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = write_warm_dry_run(project / "saved")
    (folder / "spot-0" / "nws.json").unlink()  # alerts can't be checked

    assert main(["score", "--fixtures", str(folder)]) == 0

    out = capsys.readouterr().out
    assert row("2026-06-15", "hiking", "NOT GOOD") in out
    assert "alerts: unknown" in out


@respx.mock
def test_score_live_fetches_then_scores(capsys: pytest.CaptureFixture[str]) -> None:
    forecast_route, alerts_route = mock_ok()

    assert main(["score"]) == 0

    assert forecast_route.call_count == alerts_route.call_count == 2
    assert "2 spot(s) scored" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        # The first min_run_hours in the file is hiking's.
        ("min_run_hours: 4", "min_run_hours: 0", "thresholds.hiking.min_run_hours"),
        (
            "  downhill_skiing:\n    max_temperature_f: 32\n    max_wind_mph: 10",
            "  downhill_skiing:\n    max_temperature_f: 32\n    max_wind_mph: fast",
            "thresholds.downhill_skiing.max_wind_mph",
        ),
        (
            "max_rain_in: 0",
            "max_rain_in: -1",
            "thresholds.cross_country_skiing.max_rain_in",
        ),
    ],
)
def test_score_with_bad_thresholds_exits_2_naming_the_field(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    old: str,
    new: str,
    field: str,
) -> None:
    text = SPOTS_YAML.replace(old, new, 1)
    assert text != SPOTS_YAML  # the edit really changed something
    (project / "spots.yaml").write_text(text)

    assert main(["score"]) == 2

    err = capsys.readouterr().err
    assert err.startswith(f"config error: {field} ")


@respx.mock
def test_score_checks_thresholds_before_fetching(project: Path) -> None:
    forecast_route, alerts_route = mock_ok()
    (project / "spots.yaml").write_text(
        SPOTS_YAML.replace("max_rain_in: 0", "max_rain_in: -1")
    )

    assert main(["score"]) == 2

    assert forecast_route.call_count == alerts_route.call_count == 0


def test_score_without_a_thresholds_block_exits_2(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "spots.yaml").write_text(SPOTS_YAML.replace(THRESHOLDS_YAML, ""))

    assert main(["score"]) == 2

    assert "missing 'thresholds'" in capsys.readouterr().err


@respx.mock
def test_fetch_does_not_need_thresholds(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_ok()
    (project / "spots.yaml").write_text(SPOTS_YAML.replace(THRESHOLDS_YAML, ""))

    assert main(["fetch"]) == 0


def test_score_has_no_save_raw_option() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["score", "--save-raw"])

    assert exc.value.code == 2


def test_score_fixtures_must_be_a_directory(project: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["score", "--fixtures", str(project / "nope")])

    assert exc.value.code == 2


def test_score_unexpected_error_exits_70(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "score_conditions", explode)
    folder = write_warm_dry_run(project / "saved")

    assert main(["score", "--fixtures", str(folder)]) == 70


def test_score_runs_as_a_module(project: Path) -> None:
    folder = write_warm_dry_run(project / "saved")

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "gooddays",
            "score",
            "--fixtures",
            str(folder),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0
    assert len(json.loads(done.stdout)["scores"]) == 2
    assert "level=INFO" in done.stderr


def xc_saved_run(folder: Path) -> Path:
    """A saved run of one cold, snowy, calm, dry day for a cross-country spot."""
    data = load_fixture("open_meteo_warm_dry.json")
    n = len(data["hourly"]["time"])
    data["hourly"]["temperature_2m"] = [20.0] * n
    data["hourly"]["snow_depth"] = [0.5] * n  # feet: 6 inches
    data["hourly"]["rain"] = [0.0] * n
    (folder / "spot-x").mkdir(parents=True)
    (folder / "spot-x" / "open_meteo.json").write_text(json.dumps(data))
    (folder / "spot-x" / "nws.json").write_text(
        json.dumps(load_fixture("nws_alerts_none.json"))
    )
    (folder / "meta.json").write_text(
        json.dumps({"fetched_at": "2026-06-15T04:00:00-04:00"})
    )
    return folder


CROSS_COUNTRY_SPOTS_YAML = (
    """
settings:
  timezone: America/New_York
spots:
  - {id: spot-x, name: Spot X, lat: 40.0, lon: -75.0, activities: [hiking, cross_country_skiing]}
"""
    + THRESHOLDS_YAML
)


def test_score_includes_cross_country_skiing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "spots.yaml").write_text(CROSS_COUNTRY_SPOTS_YAML)
    folder = xc_saved_run(project / "saved")

    assert main(["score", "--fixtures", str(folder)]) == 0

    out = capsys.readouterr().out
    assert row("2026-06-15", "cross_country_skiing", "GOOD") in out
    assert row("2026-06-15", "hiking", "NOT GOOD") in out  # too cold to hike
    assert (
        "temperature_f: 0/"
        not in out.split("cross_country_skiing")[1].split("hiking")[0]
    )


def test_score_json_labels_activities_with_the_config_key(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "spots.yaml").write_text(CROSS_COUNTRY_SPOTS_YAML)
    folder = xc_saved_run(project / "saved")

    assert main(["score", "--fixtures", str(folder), "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    activities = {s["activity"] for s in data["scores"][0]["scores"]}
    assert activities == {"hiking", "cross_country_skiing"}
    xc = [
        s
        for s in data["scores"][0]["scores"]
        if s["activity"] == "cross_country_skiing"
    ]
    assert {c["name"] for c in xc[0]["checks"]} >= {
        "snow_depth_in",
        "rain_in",
        "temperature_f",
    }


def test_old_skiing_activity_in_config_exits_2(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "spots.yaml").write_text(
        SPOTS_YAML.replace("[downhill_skiing]", "[skiing]")
    )

    assert main(["score"]) == 2

    assert "'skiing' was renamed" in capsys.readouterr().err


def test_old_skiing_thresholds_block_exits_2(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "spots.yaml").write_text(
        SPOTS_YAML.replace("  downhill_skiing:\n", "  skiing:\n")
    )

    assert main(["score"]) == 2

    assert "thresholds: 'skiing' was renamed" in capsys.readouterr().err


def test_a_temperature_window_with_no_room_exits_2(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "spots.yaml").write_text(
        SPOTS_YAML.replace("min_temperature_f: 10", "min_temperature_f: 32")
    )

    assert main(["score"]) == 2

    assert "must be below thresholds.cross_country_skiing.max_temperature_f" in (
        capsys.readouterr().err
    )


def test_replaying_a_folder_saved_before_rain_and_snow_depth_existed(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "spots.yaml").write_text(CROSS_COUNTRY_SPOTS_YAML)
    folder = xc_saved_run(project / "saved")
    path = folder / "spot-x" / "open_meteo.json"
    data = json.loads(path.read_text())
    del data["hourly"]["rain"], data["hourly"]["snow_depth"]
    path.write_text(json.dumps(data))

    assert main(["score", "--fixtures", str(folder)]) == 0

    out = capsys.readouterr().out
    assert row("2026-06-15", "cross_country_skiing", "NOT GOOD") in out
    assert "snow_depth_in:" in out and "no values for this reading in the data" in out


# --- message ---

# Facts for the saved warm, dry run: Spot 0 (hiking) is good on both days; Spot 1
# (downhill skiing) is not. The numbers below come from that fixture.
WARM_DRY_TEXT = (
    "Today Mon Jun 15: Spot 0 hiking, 65 F, wind 5 mph.\n"
    "Tomorrow Tue Jun 16: Spot 0 hiking, 65 F, wind 5 mph."
)


def fake_writer(text: str = WARM_DRY_TEXT, error: Exception | None = None) -> Any:
    calls: list[tuple[str, int]] = []

    def write(facts: str, max_chars: int) -> str:
        calls.append((facts, max_chars))
        if error is not None:
            raise error
        return text

    write.calls = calls  # type: ignore[attr-defined]
    return write


def use_writer(monkeypatch: pytest.MonkeyPatch, writer: Any) -> None:
    monkeypatch.setattr(cli, "anthropic_writer", lambda: writer)


def test_message_no_ai_prints_the_code_built_text(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = write_warm_dry_run(project / "saved")

    assert main(["message", "--no-ai", "--fixtures", str(folder)]) == 0

    out = capsys.readouterr().out
    assert out.splitlines() == [
        "Today Mon Jun 15:",
        "Spot 0 hiking: 65 F, wind 5 mph, daylight 5:25-20:35",
        "Tomorrow Tue Jun 16:",
        "Spot 0 hiking: 65 F, wind 5 mph, daylight 5:25-20:36",
    ]
    assert "Spot 1" not in out  # not a good day, so not mentioned


def test_message_with_ai_prints_the_ai_text(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_warm_dry_run(project / "saved")
    writer = fake_writer()
    use_writer(monkeypatch, writer)

    assert main(["message", "--fixtures", str(folder)]) == 0

    assert capsys.readouterr().out.rstrip("\n") == WARM_DRY_TEXT
    ((facts, max_chars),) = writer.calls
    assert json.loads(facts)["entries"][0]["spot"] == "Spot 0"
    assert max_chars == MESSAGE_CAP


def test_message_falls_back_when_there_is_no_key(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    folder = write_warm_dry_run(project / "saved")
    use_writer(monkeypatch, fake_writer(error=WriterError("no_credentials")))

    with caplog.at_level(logging.INFO):
        assert main(["message", "--fixtures", str(folder)]) == 0

    assert "Spot 0 hiking: 65 F" in capsys.readouterr().out  # the fallback text
    assert "message_fallback reason='no_credentials'" in caplog.text


def test_message_no_ai_never_builds_a_writer(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden() -> Any:
        raise AssertionError("--no-ai must not build the Claude writer")

    monkeypatch.setattr(cli, "anthropic_writer", forbidden)
    folder = write_warm_dry_run(project / "saved")

    assert main(["message", "--no-ai", "--fixtures", str(folder)]) == 0


def test_message_with_nothing_good_says_so_and_never_calls_the_writer(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = fake_writer()
    use_writer(monkeypatch, writer)
    folder = write_saved_run(project / "saved")  # sunny fixture, no good days today

    assert main(["message", "--fixtures", str(folder)]) == 0

    assert capsys.readouterr().out == "nothing to send\n"
    assert writer.calls == []


def test_message_when_every_spot_fails_exits_1(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "empty").mkdir()

    assert main(["message", "--no-ai", "--fixtures", str(project / "empty")]) == 1

    assert capsys.readouterr().out == "nothing to send\n"


def test_message_adds_a_footer_for_spots_that_could_not_be_checked(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_warm_dry_run(project / "saved")
    (folder / "spot-1" / "open_meteo.json").unlink()  # spot-1 can't be checked
    use_writer(monkeypatch, fake_writer())

    assert main(["message", "--fixtures", str(folder)]) == 0

    out = capsys.readouterr().out
    assert out == WARM_DRY_TEXT + "\n1 spot couldn't be checked\n"


def test_message_with_bad_thresholds_exits_2(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "spots.yaml").write_text(
        SPOTS_YAML.replace("max_rain_in: 0", "max_rain_in: -1")
    )

    assert main(["message", "--no-ai"]) == 2

    assert (
        "config error: thresholds.cross_country_skiing.max_rain_in"
        in capsys.readouterr().err
    )


def test_message_fixtures_must_be_a_directory(project: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["message", "--fixtures", str(project / "nope")])

    assert exc.value.code == 2


def test_message_has_no_json_option() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["message", "--json"])

    assert exc.value.code == 2


def test_message_unexpected_error_exits_70(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "generate_message", explode)
    folder = write_warm_dry_run(project / "saved")

    assert main(["message", "--no-ai", "--fixtures", str(folder)]) == 70


def test_message_runs_as_a_module(project: Path) -> None:
    folder = write_warm_dry_run(project / "saved")

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "gooddays",
            "message",
            "--no-ai",
            "--fixtures",
            str(folder),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0
    assert done.stdout.startswith("Today Mon Jun 15:")
    assert "message_fallback reason='writer_disabled'" in done.stderr
    assert "level=" not in done.stdout  # logs stay on stderr


def test_message_help_mentions_the_key_and_no_ai() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "gooddays", "message", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0
    assert "--no-ai" in done.stdout and "ANTHROPIC_API_KEY" in done.stdout


# --- send ---

TO = "+14155550123"
SMS_ENV = {
    "GOODDAYS_SMS_TO": TO,
    "GOODDAYS_SMS_FROM": "+15005550006",
    "TWILIO_ACCOUNT_SID": "ACtest0000000000000000000000000000",
    "TWILIO_AUTH_TOKEN": "secret-token-value",
}
# What the saved warm, dry run turns into without Claude (see the message tests).
SEND_BODY = (
    "Today Mon Jun 15:\n"
    "Spot 0 hiking: 65 F, wind 5 mph, daylight 5:25-20:35\n"
    "Tomorrow Tue Jun 16:\n"
    "Spot 0 hiking: 65 F, wind 5 mph, daylight 5:25-20:36"
)


class FakeSms:
    """A fake SmsSender: each call returns the next outcome (an id or an error)."""

    def __init__(self, *outcomes: str | SendError) -> None:
        self.outcomes = list(outcomes) or ["SM123"]
        self.calls: list[tuple[str, str]] = []

    def __call__(self, to: str, body: str) -> str:
        self.calls.append((to, body))
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, SendError):
            raise outcome
        return outcome


def use_sms(monkeypatch: pytest.MonkeyPatch, sender: Any) -> None:
    monkeypatch.setattr(cli, "sms_sender", lambda: sender)


def forbid_sms(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> Any:
        raise AssertionError("the SMS sender must not be built here")

    monkeypatch.setattr(cli, "sms_sender", forbidden)


def sms_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    for name, value in {**SMS_ENV, **overrides}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def send_args(folder: Path, *extra: str) -> list[str]:
    return ["send", "--no-ai", "--fixtures", str(folder), *extra]


def test_send_sends_the_text_once_unchanged(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    folder = write_warm_dry_run(project / "saved")
    sms_env(monkeypatch)
    sms = FakeSms("SM777")
    use_sms(monkeypatch, sms)

    with caplog.at_level(logging.INFO):
        assert main(send_args(folder)) == 0

    out, err = capsys.readouterr()
    assert sms.calls == [(TO, SEND_BODY)]
    assert out.splitlines()[:-1] == SEND_BODY.splitlines()  # the text, then the status
    assert out.splitlines()[-1] == (
        "sent to +***0123 (accepted by the provider, not confirmed delivered; "
        "id=SM777, attempts=1, segments=1)"
    )
    assert "sending a real SMS to +***0123" in err
    assert "running this again sends the same text again" in err
    # The full number and the body stay out of every output and log.
    assert (
        TO not in out + err + caplog.text and "5550123" not in out + err + caplog.text
    )
    assert SEND_BODY not in caplog.text


def test_send_dry_run_prints_the_text_and_needs_nothing(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_warm_dry_run(project / "saved")
    forbid_sms(monkeypatch)  # no sender, no phone number, no credentials

    assert main(send_args(folder, "--dry-run")) == 0

    out, err = capsys.readouterr()
    assert out.rstrip("\n") == SEND_BODY
    assert "dry run: not sent (1 segment(s))" in err


def test_send_dry_run_is_allowed_in_ci(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_warm_dry_run(project / "saved")
    forbid_sms(monkeypatch)
    monkeypatch.setenv("CI", "true")

    assert main(send_args(folder, "--dry-run")) == 0
    assert capsys.readouterr().out.rstrip("\n") == SEND_BODY


@pytest.mark.parametrize("value", ["true", "1", "false"])  # any non-empty value counts
def test_send_refuses_to_send_for_real_in_ci(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    folder = write_warm_dry_run(project / "saved")
    sms_env(monkeypatch)
    forbid_sms(monkeypatch)
    monkeypatch.setenv("CI", value)

    assert main(send_args(folder)) == 2

    assert "refusing to send a real SMS when CI is set" in capsys.readouterr().err


def test_send_with_nothing_to_send_sends_nothing_and_needs_no_settings(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_sms(monkeypatch)  # no good days: no sender, and no phone number is read
    folder = write_saved_run(project / "saved")

    assert main(send_args(folder)) == 0

    assert capsys.readouterr().out == "nothing to send\n"


def test_send_when_every_spot_failed_exits_1_and_sends_nothing(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sms_env(monkeypatch)
    forbid_sms(monkeypatch)
    (project / "empty").mkdir()

    assert main(send_args(project / "empty")) == 1

    assert capsys.readouterr().out == "nothing to send\n"


def test_send_without_a_recipient_exits_2_naming_the_setting(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_warm_dry_run(project / "saved")
    sms_env(monkeypatch, GOODDAYS_SMS_TO=None)
    forbid_sms(monkeypatch)

    assert main(send_args(folder)) == 2

    assert "GOODDAYS_SMS_TO" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad", ["2125559876", "+0155550123", "+1212", "call me", "+1 212 555 9876"]
)
def test_send_with_an_invalid_recipient_exits_2_without_echoing_it(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    folder = write_warm_dry_run(project / "saved")
    sms_env(monkeypatch, GOODDAYS_SMS_TO=bad)
    forbid_sms(monkeypatch)

    assert main(send_args(folder)) == 2

    out, err = capsys.readouterr()
    assert "GOODDAYS_SMS_TO" in err and bad not in out + err


def test_send_with_missing_credentials_exits_2_naming_them(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_warm_dry_run(project / "saved")
    sms_env(
        monkeypatch,
        TWILIO_ACCOUNT_SID=None,
        TWILIO_AUTH_TOKEN=None,
        GOODDAYS_SMS_FROM=None,
    )

    assert main(send_args(folder)) == 2  # the real sender loader, no client made

    err = capsys.readouterr().err
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "GOODDAYS_SMS_FROM"):
        assert name in err
    assert TO not in err


def test_forgetting_to_fake_the_sender_cannot_send_for_real(
    project: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Everything is configured but the test did not fake the sender: the autouse
    # guard stops it before any client exists (an unexpected error, exit 70).
    folder = write_warm_dry_run(project / "saved")
    sms_env(monkeypatch)

    with caplog.at_level(logging.ERROR):
        assert main(send_args(folder)) == 70

    assert "fake the sender" in caplog.text


def test_send_failure_prints_the_text_and_exits_4(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_warm_dry_run(project / "saved")
    sms_env(monkeypatch)
    sms = FakeSms(SendError("server_error", retryable=True))
    use_sms(monkeypatch, sms)

    assert main(send_args(folder)) == 4

    out, err = capsys.readouterr()
    assert out.rstrip("\n") == SEND_BODY  # the text is not lost
    assert "error: SMS not sent (reason=server_error, attempts=2)" in err
    assert len(sms.calls) == 2  # one retry, then it gives up
    assert "sent to" not in out


def test_send_failure_after_a_read_timeout_says_it_may_have_arrived(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_warm_dry_run(project / "saved")
    sms_env(monkeypatch)
    sms = FakeSms(SendError("read_timeout"))
    use_sms(monkeypatch, sms)

    assert main(send_args(folder)) == 4

    err = capsys.readouterr().err
    assert (
        "reason=read_timeout, attempts=1" in err
        and "may still have been delivered" in err
    )
    assert len(sms.calls) == 1  # never retried: it could text twice


def test_send_succeeds_after_a_retry(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_warm_dry_run(project / "saved")
    sms_env(monkeypatch)
    use_sms(monkeypatch, FakeSms(SendError("connection_error", retryable=True), "SM5"))

    assert main(send_args(folder)) == 0

    assert "id=SM5, attempts=2" in capsys.readouterr().out


@respx.mock
def test_send_end_to_end_through_the_real_twilio_adapter(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    real_sms_client: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from urllib.parse import parse_qs

    folder = write_warm_dry_run(project / "saved")
    sms_env(monkeypatch)
    real_sms_client()  # the real adapter and httpx client, under respx
    url = f"https://api.twilio.com/2010-04-01/Accounts/{SMS_ENV['TWILIO_ACCOUNT_SID']}/Messages.json"
    route = respx.post(url).mock(
        return_value=httpx.Response(201, json={"sid": "SMabc", "status": "queued"})
    )

    with caplog.at_level(logging.INFO):
        assert main(send_args(folder)) == 0

    (call,) = route.calls
    assert parse_qs(call.request.content.decode()) == {
        "To": [TO],
        "From": [SMS_ENV["GOODDAYS_SMS_FROM"]],
        "Body": [SEND_BODY],
    }
    out, err = capsys.readouterr()
    assert "id=SMabc" in out
    everything = out + err + caplog.text
    assert SMS_ENV["TWILIO_AUTH_TOKEN"] not in everything
    assert SMS_ENV["TWILIO_ACCOUNT_SID"] not in everything  # httpx's URL log stays off
    assert TO not in everything


def test_message_never_builds_a_sender(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = write_warm_dry_run(project / "saved")
    sms_env(monkeypatch)
    forbid_sms(monkeypatch)

    assert main(["message", "--no-ai", "--fixtures", str(folder)]) == 0


def test_send_unexpected_error_exits_70(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "generate_message", explode)
    folder = write_warm_dry_run(project / "saved")

    assert main(send_args(folder, "--dry-run")) == 70


def test_send_with_bad_thresholds_exits_2_before_anything_is_sent(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "spots.yaml").write_text(
        SPOTS_YAML.replace("max_rain_in: 0", "max_rain_in: -1")
    )
    forbid_sms(monkeypatch)

    assert main(["send", "--no-ai", "--dry-run"]) == 2

    assert (
        "config error: thresholds.cross_country_skiing.max_rain_in"
        in capsys.readouterr().err
    )


def test_send_fixtures_must_be_a_directory(project: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["send", "--fixtures", str(project / "nope")])

    assert exc.value.code == 2


def test_send_help_describes_the_real_send_and_exit_code_4(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["send", "--help"])

    out = capsys.readouterr().out
    assert exc.value.code == 0
    for text in (
        "--dry-run",
        "GOODDAYS_SMS_TO",
        "TWILIO_AUTH_TOKEN",
        "4 when the text",
    ):
        assert text in " ".join(out.split())


def test_send_does_not_import_the_twilio_module_for_other_commands(
    project: Path,
) -> None:
    folder = write_warm_dry_run(project / "saved")
    code = (
        "import sys\n"
        "from gooddays.cli import main\n"
        f"code = main(['message', '--no-ai', '--fixtures', {str(folder)!r}])\n"
        "print('CODE', code, 'sms_sender' in sys.modules)\n"
    )

    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert "CODE 0 False" in done.stdout, done.stderr
