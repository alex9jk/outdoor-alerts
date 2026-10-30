import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

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


@respx.mock
def test_save_raw_writes_todays_folder(project: Path) -> None:
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
    assert (
        replay_out.splitlines()[1:] == live_out.splitlines()[1:]
    )  # same but "fetched"


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


def test_missing_spots_file_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch", "--spots", "nope.yaml"]) == 2

    assert "config error: cannot read nope.yaml" in capsys.readouterr().err


def test_missing_user_agent_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(USER_AGENT_ENV_VAR)

    assert main(["fetch"]) == 2

    assert USER_AGENT_ENV_VAR in capsys.readouterr().err


@respx.mock
def test_custom_spots_path(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mock_ok()
    other = project / "other.yaml"
    other.write_text(SPOTS_YAML.replace("spot-1", "spot-9").replace("Spot 1", "Spot 9"))

    assert main(["fetch", "--spots", str(other)]) == 0

    assert "Spot 9 (spot-9)" in capsys.readouterr().out


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])

    assert exc.value.code == 2


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
