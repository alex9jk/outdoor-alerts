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
from gooddays.storage import USER_AGENT_ENV_VAR
from helpers import load_fixture

SPOTS_YAML = """
settings:
  timezone: America/New_York
spots:
  - {id: spot-0, name: Spot 0, lat: 40.0, lon: -75.0, activities: [hiking]}
  - {id: spot-1, name: Spot 1, lat: 41.0, lon: -75.0, activities: [skiing]}
"""


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
