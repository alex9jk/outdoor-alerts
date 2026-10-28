"""Where spots and settings come from.

The pipeline depends on the `Storage` protocol, not on YAML, so a
DynamoDB-backed implementation can replace `LocalStorage` later.
"""

from pathlib import Path
from typing import Any, Protocol, get_args

import yaml

from gooddays.models import Activity, Settings, Spot


class ConfigError(ValueError):
    """spots.yaml is missing, malformed, or has invalid values."""


class Storage(Protocol):
    def get_settings(self) -> Settings: ...

    def get_spots(self) -> list[Spot]: ...


class LocalStorage:
    """Reads settings and spots from a YAML file (default: spots.yaml)."""

    def __init__(self, path: Path = Path("spots.yaml")) -> None:
        self._path = path

    def get_settings(self) -> Settings:
        raw = self._load().get("settings")
        if not isinstance(raw, dict):
            raise ConfigError("missing 'settings' mapping")
        return Settings(
            nws_user_agent=_require(raw, "nws_user_agent", str, "settings"),
            timezone=_require(raw, "timezone", str, "settings"),
        )

    def get_spots(self) -> list[Spot]:
        raw = self._load().get("spots")
        if not isinstance(raw, list) or not raw:
            raise ConfigError("'spots' must be a non-empty list")
        spots = [_parse_spot(i, item) for i, item in enumerate(raw)]
        seen: set[str] = set()
        for spot in spots:
            if spot.id in seen:
                raise ConfigError(f"duplicate spot id: {spot.id!r}")
            seen.add(spot.id)
        return spots

    def _load(self) -> dict[str, Any]:
        try:
            data = yaml.safe_load(self._path.read_text())
        except OSError as e:
            raise ConfigError(f"cannot read {self._path}: {e}") from e
        except yaml.YAMLError as e:
            raise ConfigError(f"invalid YAML in {self._path}: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"{self._path} must contain a mapping at the top level")
        return data


def _parse_spot(index: int, item: object) -> Spot:
    where = f"spots[{index}]"
    if not isinstance(item, dict):
        raise ConfigError(f"{where} must be a mapping")
    lat = _require_number(item, "lat", where)
    lon = _require_number(item, "lon", where)
    if not -90 <= lat <= 90:
        raise ConfigError(f"{where}.lat out of range: {lat}")
    if not -180 <= lon <= 180:
        raise ConfigError(f"{where}.lon out of range: {lon}")

    activities = item.get("activities")
    valid = get_args(Activity)
    if (
        not isinstance(activities, list)
        or not activities
        or any(a not in valid for a in activities)
    ):
        raise ConfigError(f"{where}.activities must be a non-empty list of {valid}")

    return Spot(
        id=_require(item, "id", str, where),
        name=_require(item, "name", str, where),
        lat=lat,
        lon=lon,
        activities=tuple(activities),
    )


def _require[T](raw: dict[str, Any], key: str, typ: type[T], where: str) -> T:
    value = raw.get(key)
    if not isinstance(value, typ) or (isinstance(value, str) and not value.strip()):
        raise ConfigError(f"{where}.{key} must be a non-empty {typ.__name__}")
    return value


def _require_number(raw: dict[str, Any], key: str, where: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}.{key} must be a number")
    return float(value)
