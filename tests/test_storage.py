import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from gooddays.models import HikingThresholds, SkiingThresholds
from gooddays.storage import USER_AGENT_ENV_VAR, ConfigError, LocalStorage

SETTINGS = """
settings:
  nws_user_agent: "test (test@example.com)"
  timezone: America/New_York
"""
SPOT = "  - {id: a, name: Spot A, lat: 41.0, lon: -74.0, activities: [hiking]}\n"
VALID = SETTINGS + "spots:\n" + SPOT


@pytest.fixture(autouse=True)
def no_env_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)


def write(tmp_path: Path, text: str) -> LocalStorage:
    path = tmp_path / "spots.yaml"
    path.write_text(text)
    return LocalStorage(path)


def spot_with(**overrides: str) -> str:
    fields = {
        "id": "a",
        "name": "Spot A",
        "lat": "41.0",
        "lon": "-74.0",
        "activities": "[hiking]",
    }
    fields.update(overrides)
    body = ", ".join(f"{k}: {v}" for k, v in fields.items())
    return SETTINGS + f"spots:\n  - {{{body}}}\n"


def test_loads_settings_and_spots(tmp_path: Path) -> None:
    storage = write(tmp_path, VALID.replace("[hiking]", "[hiking, skiing]"))
    settings = storage.get_settings()
    assert settings.timezone == "America/New_York"
    assert settings.nws_user_agent == "test (test@example.com)"
    (spot,) = storage.get_spots()
    assert spot.id == "a"
    assert spot.activities == ("hiking", "skiing")


def test_repo_spots_yaml_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "test (test@example.com)")
    storage = LocalStorage(Path(__file__).parent.parent / "spots.yaml")
    storage.get_settings()
    assert storage.get_spots()


def test_env_var_overrides_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "from-env (env@example.com)")
    settings = write(tmp_path, VALID).get_settings()
    assert settings.nws_user_agent == "from-env (env@example.com)"


def test_env_var_alone_is_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "from-env (env@example.com)")
    text = "settings:\n  timezone: America/New_York\nspots:\n" + SPOT
    assert write(tmp_path, text).get_settings().nws_user_agent.startswith("from-env")


def test_user_agent_missing_everywhere(tmp_path: Path) -> None:
    text = "settings:\n  timezone: America/New_York\nspots:\n" + SPOT
    with pytest.raises(ConfigError, match=USER_AGENT_ENV_VAR):
        write(tmp_path, text).get_settings()


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        LocalStorage(tmp_path / "nope.yaml").get_spots()


def test_invalid_yaml_syntax(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        write(tmp_path, "spots: [unclosed\n").get_spots()


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", ""])
def test_top_level_not_a_mapping(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match="mapping at the top level"):
        write(tmp_path, text).get_spots()


@pytest.mark.parametrize("spots", ["spots: []\n", "spots: nope\n", ""])
def test_spots_empty_or_not_a_list(tmp_path: Path, spots: str) -> None:
    with pytest.raises(ConfigError, match="non-empty list"):
        write(tmp_path, SETTINGS + spots).get_spots()


def test_spot_entry_not_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"spots\[0\] must be a mapping"):
        write(tmp_path, SETTINGS + "spots:\n  - just-a-string\n").get_spots()


def test_duplicate_ids(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="duplicate spot id: 'a'"):
        write(tmp_path, VALID + SPOT).get_spots()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"lat": "91"}, r"lat out of range"),
        ({"lon": "-181"}, r"lon out of range"),
        ({"lat": "'41.0'"}, r"lat must be a number"),
        ({"lon": "true"}, r"lon must be a number"),
        ({"activities": "[swimming]"}, r"activities must be"),
        ({"activities": "[]"}, r"activities must be"),
        ({"activities": "hiking"}, r"activities must be"),
        ({"name": "''"}, r"name must be a non-empty"),
        ({"id": "''"}, r"id must be a non-empty"),
        ({"id": "../x"}, r"id must be a lowercase slug"),
        ({"id": "Bear_Mountain"}, r"id must be a lowercase slug"),
        ({"id": "a--b"}, r"id must be a lowercase slug"),
        ({"id": "'a b'"}, r"id must be a lowercase slug"),
    ],
)
def test_invalid_spot_values(
    tmp_path: Path, override: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        write(tmp_path, spot_with(**override)).get_spots()


@pytest.mark.parametrize("missing", ["id", "name", "lat", "lon", "activities"])
def test_missing_spot_field(tmp_path: Path, missing: str) -> None:
    body = {
        "id": "a",
        "name": "A",
        "lat": "1",
        "lon": "1",
        "activities": "[hiking]",
    }
    del body[missing]
    entry = ", ".join(f"{k}: {v}" for k, v in body.items())
    text = SETTINGS + f"spots:\n  - {{{entry}}}\n"
    with pytest.raises(ConfigError, match=missing):
        write(tmp_path, text).get_spots()


def test_missing_settings(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="settings"):
        write(tmp_path, "spots: []\n").get_settings()


@pytest.mark.parametrize("body", ["  nws_user_agent: ''\n", "  timezone: ''\n"])
def test_empty_settings_values(tmp_path: Path, body: str) -> None:
    text = "settings:\n  timezone: America/New_York\n" + body + "spots:\n" + SPOT
    with pytest.raises(ConfigError):
        write(tmp_path, text).get_settings()


@pytest.mark.parametrize("name", ["America/Nowhere", "Eastern", "../etc/passwd", "."])
def test_invalid_timezone(tmp_path: Path, name: str) -> None:
    text = f"settings:\n  timezone: '{name}'\n  nws_user_agent: x\nspots:\n" + SPOT
    with pytest.raises(ConfigError, match="valid IANA timezone"):
        write(tmp_path, text).get_settings()


def test_non_ascii_user_agent_from_yaml(tmp_path: Path) -> None:
    text = (
        "settings:\n"
        "  timezone: America/New_York\n"
        "  nws_user_agent: 'Café (me@example.com)'\n"
        "spots:\n" + SPOT
    )
    with pytest.raises(ConfigError, match="must be ASCII"):
        write(tmp_path, text).get_settings()


def test_non_ascii_user_agent_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "Café (me@example.com)")
    with pytest.raises(ConfigError, match="must be ASCII"):
        write(tmp_path, VALID).get_settings()


def test_spots_file_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "spots.yaml"
    path.write_bytes(b"\xff\xfe spots: []")

    with pytest.raises(ConfigError, match="not valid UTF-8"):
        LocalStorage(path).get_spots()


# --- thresholds ---

THRESHOLDS_DICT: dict[str, Any] = {
    "hiking": {
        "min_temperature_f": 45,
        "max_precipitation_probability_pct": 30,
        "max_precipitation_in": 0.1,
        "min_run_hours": 4,
    },
    "skiing": {
        "max_temperature_f": 32,
        "max_wind_mph": 10,
        "min_snowfall_in": 0,
        "min_snow_depth_in": 0,
        "min_run_hours": 4,
    },
}


def write_thresholds(tmp_path: Path, thresholds: Any) -> LocalStorage:
    doc: dict[str, Any] = {"spots": [], "settings": {}}
    if thresholds is not None:
        doc["thresholds"] = thresholds
    return write(tmp_path, yaml.safe_dump(doc))


def with_change(path: list[str], value: Any) -> dict[str, Any]:
    """THRESHOLDS_DICT with the value at `path` replaced (or removed if `...`)."""
    changed = copy.deepcopy(THRESHOLDS_DICT)
    node = changed
    for key in path[:-1]:
        node = node[key]
    if value is ...:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return changed


def test_loads_thresholds(tmp_path: Path) -> None:
    thresholds = write_thresholds(tmp_path, THRESHOLDS_DICT).get_thresholds()

    assert thresholds.hiking == HikingThresholds(45, 30, 0.1, 4)
    assert thresholds.skiing == SkiingThresholds(32, 10, 0, 0, 4)


def test_repo_spots_yaml_thresholds_are_valid() -> None:
    storage = LocalStorage(Path(__file__).parent.parent / "spots.yaml")

    thresholds = storage.get_thresholds()

    assert thresholds.hiking.min_temperature_f == 45
    assert thresholds.skiing.max_temperature_f == 32


@pytest.mark.parametrize(
    ("thresholds", "message"),
    [
        (None, "missing 'thresholds'"),
        ("hiking", "missing 'thresholds'"),
        (with_change(["hiking"], ...), "missing 'thresholds.hiking'"),
        (with_change(["skiing"], ...), "missing 'thresholds.skiing'"),
        (
            with_change(["hiking", "min_temperature_f"], ...),
            "hiking.min_temperature_f must be a number",
        ),
        (
            with_change(["skiing", "max_wind_mph"], ...),
            "skiing.max_wind_mph must be a number",
        ),
        (
            with_change(["skiing", "min_snow_depth_in"], ...),
            "min_snow_depth_in must be a number",
        ),
        (
            with_change(["hiking", "min_temperature_f"], "warm"),
            "min_temperature_f must be a number",
        ),
        (
            with_change(["hiking", "min_temperature_f"], True),
            "min_temperature_f must be a number",
        ),
        (
            with_change(["hiking", "min_temperature_f"], float("inf")),
            "must be a finite number",
        ),
        (
            with_change(["hiking", "max_precipitation_probability_pct"], 101),
            "at most 100",
        ),
        (
            with_change(["hiking", "max_precipitation_probability_pct"], -1),
            "at least 0",
        ),
        (with_change(["hiking", "max_precipitation_in"], -0.1), "at least 0"),
        (with_change(["skiing", "max_wind_mph"], -1), "at least 0"),
        (with_change(["skiing", "min_snowfall_in"], -1), "at least 0"),
        (with_change(["skiing", "min_snow_depth_in"], -1), "at least 0"),
        (
            with_change(["hiking", "min_run_hours"], ...),
            "min_run_hours must be a whole number",
        ),
        (
            with_change(["hiking", "min_run_hours"], 0),
            "min_run_hours must be a whole number",
        ),
        (
            with_change(["skiing", "min_run_hours"], 2.5),
            "min_run_hours must be a whole number",
        ),
        (
            with_change(["skiing", "min_run_hours"], True),
            "min_run_hours must be a whole number",
        ),
        (with_change(["hiking", "min_temperatur_f"], 45), "unknown key"),
    ],
)
def test_invalid_thresholds(tmp_path: Path, thresholds: Any, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        write_thresholds(tmp_path, thresholds).get_thresholds()


def test_unknown_keys_are_reported_before_missing_ones(tmp_path: Path) -> None:
    # The typo'd key is reported as unknown before the missing one.
    changed = with_change(["hiking", "min_temperature_f"], ...)
    changed["hiking"]["min_temperatur_f"] = 45

    with pytest.raises(ConfigError, match=r"unknown key\(s\): \['min_temperatur_f'\]"):
        write_thresholds(tmp_path, changed).get_thresholds()


def test_biking_thresholds_are_refused(tmp_path: Path) -> None:
    changed = {**THRESHOLDS_DICT, "biking": {"min_temperature_f": 50}}

    with pytest.raises(ConfigError, match="no biking rules yet"):
        write_thresholds(tmp_path, changed).get_thresholds()


def test_unknown_activity_thresholds_are_refused(tmp_path: Path) -> None:
    changed = {**THRESHOLDS_DICT, "surfing": {}}

    with pytest.raises(ConfigError, match=r"unknown activities: \['surfing'\]"):
        write_thresholds(tmp_path, changed).get_thresholds()
