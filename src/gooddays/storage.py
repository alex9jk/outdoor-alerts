"""Where spots and settings come from.

The pipeline depends on the `Storage` protocol, not on YAML, so a
DynamoDB-backed implementation can replace `LocalStorage` later.
"""

import math
import os
from pathlib import Path
from typing import Any, Protocol, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from gooddays.models import (
    SPOT_ID_PATTERN,
    Activity,
    CrossCountrySkiingThresholds,
    DownhillSkiingThresholds,
    HikingThresholds,
    Settings,
    Spot,
    Thresholds,
)

USER_AGENT_ENV_VAR = "GOODDAYS_NWS_USER_AGENT"


class ConfigError(ValueError):
    """spots.yaml is missing, malformed, or has invalid values."""


class Storage(Protocol):
    def get_settings(self) -> Settings: ...

    def get_spots(self) -> list[Spot]: ...

    def get_thresholds(self) -> Thresholds: ...


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

    def get_thresholds(self) -> Thresholds:
        raw = self._load().get("thresholds")
        if raw is None:
            raise ConfigError("missing 'thresholds' mapping")
        if not isinstance(raw, dict):
            raise ConfigError("'thresholds' must be a mapping of activities")
        if "skiing" in raw:
            raise ConfigError(_skiing_renamed("thresholds"))
        if "biking" in raw:
            raise ConfigError(
                "thresholds.biking: there are no biking rules yet; remove it"
            )
        unknown = sorted(str(k) for k in set(raw) - set(_THRESHOLD_KEYS))
        if unknown:
            raise ConfigError(f"thresholds has unknown activities: {unknown}")

        blocks = {
            activity: _threshold_block(raw, activity, keys)
            for activity, keys in _THRESHOLD_KEYS.items()
        }
        hiking, downhill, xc = (
            blocks["hiking"],
            blocks["downhill_skiing"],
            blocks["cross_country_skiing"],
        )
        return Thresholds(
            hiking=HikingThresholds(
                min_temperature_f=_threshold_number(
                    hiking, "min_temperature_f", "thresholds.hiking"
                ),
                max_precipitation_probability_pct=_threshold_number(
                    hiking,
                    "max_precipitation_probability_pct",
                    "thresholds.hiking",
                    minimum=0,
                    maximum=100,
                ),
                max_precipitation_in=_threshold_number(
                    hiking, "max_precipitation_in", "thresholds.hiking", minimum=0
                ),
                min_run_hours=_run_hours(hiking, "thresholds.hiking"),
            ),
            downhill_skiing=_downhill(downhill),
            cross_country_skiing=_cross_country(xc),
        )

    def _load(self) -> dict[str, Any]:
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as e:
            raise ConfigError(f"{self._path} is not valid UTF-8: {e}") from e
        except OSError as e:
            raise ConfigError(f"cannot read {self._path}: {e}") from e
        except yaml.YAMLError as e:
            raise ConfigError(f"invalid YAML in {self._path}: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"{self._path} must contain a mapping at the top level")
        return data


_THRESHOLD_KEYS: dict[str, set[str]] = {
    "hiking": {
        "min_temperature_f",
        "max_precipitation_probability_pct",
        "max_precipitation_in",
        "min_run_hours",
    },
    "downhill_skiing": {
        "max_temperature_f",
        "max_wind_mph",
        "min_snowfall_in",
        "min_snow_depth_in",
        "min_run_hours",
    },
    "cross_country_skiing": {
        "min_snow_depth_in",
        "min_temperature_f",
        "max_temperature_f",
        "max_wind_mph",
        "max_precipitation_probability_pct",
        "max_rain_in",
        "min_run_hours",
    },
}


def _skiing_renamed(where: str) -> str:
    return (
        f"{where}: 'skiing' was renamed; "
        "use 'downhill_skiing' or 'cross_country_skiing'"
    )


def _downhill(block: dict[str, Any]) -> DownhillSkiingThresholds:
    where = "thresholds.downhill_skiing"
    return DownhillSkiingThresholds(
        max_temperature_f=_threshold_number(block, "max_temperature_f", where),
        max_wind_mph=_threshold_number(block, "max_wind_mph", where, minimum=0),
        min_snowfall_in=_threshold_number(block, "min_snowfall_in", where, minimum=0),
        min_snow_depth_in=_threshold_number(
            block, "min_snow_depth_in", where, minimum=0
        ),
        min_run_hours=_run_hours(block, where),
    )


def _cross_country(block: dict[str, Any]) -> CrossCountrySkiingThresholds:
    where = "thresholds.cross_country_skiing"
    min_temperature = _threshold_number(block, "min_temperature_f", where)
    max_temperature = _threshold_number(block, "max_temperature_f", where)
    if min_temperature >= max_temperature:
        raise ConfigError(
            f"{where}.min_temperature_f ({min_temperature:g}) must be below "
            f"{where}.max_temperature_f ({max_temperature:g})"
        )
    return CrossCountrySkiingThresholds(
        min_snow_depth_in=_threshold_number(
            block, "min_snow_depth_in", where, minimum=0
        ),
        min_temperature_f=min_temperature,
        max_temperature_f=max_temperature,
        max_wind_mph=_threshold_number(block, "max_wind_mph", where, minimum=0),
        max_precipitation_probability_pct=_threshold_number(
            block, "max_precipitation_probability_pct", where, minimum=0, maximum=100
        ),
        max_rain_in=_threshold_number(block, "max_rain_in", where, minimum=0),
        min_run_hours=_run_hours(block, where),
    )


def _threshold_block(
    raw: dict[str, Any], activity: str, keys: set[str]
) -> dict[str, Any]:
    where = f"thresholds.{activity}"
    block = raw.get(activity)
    if block is None:
        raise ConfigError(f"missing '{where}' mapping")
    if not isinstance(block, dict):
        raise ConfigError(f"'{where}' must be a mapping of limits")
    unknown = sorted(str(k) for k in set(block) - keys)
    if unknown:
        raise ConfigError(f"{where} has unknown key(s): {unknown}")
    return block


def _threshold_number(
    block: dict[str, Any],
    key: str,
    where: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = _require_number(block, key, where)
    if not math.isfinite(value):
        raise ConfigError(f"{where}.{key} must be a finite number")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}.{key} must be at least {minimum:g}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{where}.{key} must be at most {maximum:g}")
    return value


def _run_hours(block: dict[str, Any], where: str) -> int:
    value = block.get("min_run_hours")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 24:
        raise ConfigError(f"{where}.min_run_hours must be a whole number from 1 to 24")
    return value


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
    if isinstance(activities, list) and "skiing" in activities:
        raise ConfigError(_skiing_renamed(f"{where}.activities"))
    valid = get_args(Activity)
    if (
        not isinstance(activities, list)
        or not activities
        or any(a not in valid for a in activities)
    ):
        raise ConfigError(f"{where}.activities must be a non-empty list of {valid}")

    spot_id = _require(item, "id", str, where)
    if not SPOT_ID_PATTERN.fullmatch(spot_id):
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
