from pathlib import Path

import pytest

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
