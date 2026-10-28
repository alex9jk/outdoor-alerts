from pathlib import Path

import pytest

from gooddays.storage import ConfigError, LocalStorage

VALID = """
settings:
  nws_user_agent: "test (test@example.com)"
  timezone: America/New_York
spots:
  - id: a
    name: Spot A
    lat: 41.0
    lon: -74.0
    activities: [hiking, skiing]
"""


def write(tmp_path: Path, text: str) -> LocalStorage:
    path = tmp_path / "spots.yaml"
    path.write_text(text)
    return LocalStorage(path)


def test_loads_settings_and_spots(tmp_path: Path) -> None:
    storage = write(tmp_path, VALID)
    settings = storage.get_settings()
    assert settings.timezone == "America/New_York"
    (spot,) = storage.get_spots()
    assert spot.id == "a"
    assert spot.activities == ("hiking", "skiing")


def test_repo_spots_yaml_is_valid() -> None:
    storage = LocalStorage(Path(__file__).parent.parent / "spots.yaml")
    storage.get_settings()
    assert storage.get_spots()


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        LocalStorage(tmp_path / "nope.yaml").get_spots()


def test_duplicate_ids(tmp_path: Path) -> None:
    spot = "  - {id: a, name: A, lat: 1, lon: 1, activities: [hiking]}\n"
    storage = write(tmp_path, VALID.split("spots:")[0] + "spots:\n" + spot * 2)
    with pytest.raises(ConfigError, match="duplicate"):
        storage.get_spots()


@pytest.mark.parametrize(
    "bad",
    [
        "lat: 91",
        "lon: -181",
        "activities: [swimming]",
        "activities: []",
        "id: ''",
    ],
)
def test_invalid_spot_values(tmp_path: Path, bad: str) -> None:
    key = bad.split(":")[0]
    lines = [
        line for line in VALID.splitlines() if not line.strip().startswith(f"{key}:")
    ]
    text = "\n".join(lines) + f"\n    {bad}\n"
    with pytest.raises(ConfigError):
        write(tmp_path, text).get_spots()


def test_missing_settings(tmp_path: Path) -> None:
    storage = write(tmp_path, "spots: []\n")
    with pytest.raises(ConfigError, match="settings"):
        storage.get_settings()
