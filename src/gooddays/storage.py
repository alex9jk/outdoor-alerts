"""Where spots and settings come from.

The pipeline depends on the `Storage` protocol, not on YAML, so a
DynamoDB-backed implementation can replace `LocalStorage` later.
"""

import os
import re
from pathlib import Path
from typing import Any, Protocol, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from gooddays.models import Activity, Settings, Spot

USER_AGENT_ENV_VAR = "GOODDAYS_NWS_USER_AGENT"

# Spot ids end up in log lines and data/raw/{date}/{spot_id}/ paths.
_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


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
        # The env var wins so contact details needn't be committed to spots.yaml.
        user_agent = os.environ.get(USER_AGENT_ENV_VAR, "").strip()
        if not user_agent and "nws_user_agent" not in raw:
            raise ConfigError(
                f"set {USER_AGENT_ENV_VAR} or settings.nws_user_agent in {self._path}"
            )
        user_agent = user_agent or _require(raw, "nws_user_agent", str, "settings")
        if not user_agent.isascii():
            raise ConfigError("the NWS user agent must be ASCII (it is an HTTP header)")
        return Settings(
            nws_user_agent=user_agent,
            timezone=_parse_timezone(_require(raw, "timezone", str, "settings")),
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


def _parse_timezone(name: str) -> str:
    # Fail at load time on a typo, not later as an error on every spot.
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise ConfigError(
            f"settings.timezone is not a valid IANA timezone: {name!r}"
        ) from e
    return name


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

    spot_id = _require(item, "id", str, where)
    if not _SLUG.fullmatch(spot_id):
        raise ConfigError(
            f"{where}.id must be a lowercase slug like 'bear-mountain': {spot_id!r}"
        )

    return Spot(
        id=spot_id,
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
