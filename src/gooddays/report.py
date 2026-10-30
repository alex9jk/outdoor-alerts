"""Render a FetchResult for people (text) or programs (JSON).

Text shows plain per-day summaries of the fetched data. It does no scoring.
"""

import dataclasses
import json
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from gooddays.models import (
    Alert,
    DaylightWindow,
    FetchResult,
    HourlyConditions,
    SpotConditions,
    SpotError,
)


def to_json(result: FetchResult) -> str:
    """Everything in the result. `alerts` is null if unknown, [] if none active."""
    return json.dumps(_jsonable(result), indent=2)


def to_text(result: FetchResult) -> str:
    lines: list[str] = []
    for conditions in result.results:
        lines.extend(_spot_lines(conditions))
        lines.append("")
    lines.extend(_error_line(error) for error in result.errors)
    lines.append(f"{len(result.results)} spot(s) ok, {len(result.errors)} error(s)")
    return "\n".join(lines)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _spot_lines(conditions: SpotConditions) -> list[str]:
    spot = conditions.spot
    fetched = conditions.fetched_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"{spot.name} ({spot.id})  fetched {fetched}"]

    by_day: dict[date, list[HourlyConditions]] = defaultdict(list)
    for hour in conditions.hourly:
        by_day[hour.time.date()].append(hour)
    daylight = {window.date: window for window in conditions.daylight}
    for day in sorted(by_day):
        lines.append(f"  {day}  {_day_summary(by_day[day], daylight.get(day))}")

    lines.extend(_alert_lines(conditions.alerts))
    return lines


def _day_summary(hours: list[HourlyConditions], window: DaylightWindow | None) -> str:
    def values(field: str) -> list[float]:
        return [v for h in hours if (v := getattr(h, field)) is not None]

    temps = values("temperature_f")
    probs = values("precipitation_probability_pct")
    precip = values("precipitation_in")
    winds = values("wind_speed_mph")
    gusts = values("wind_gust_mph")
    snow = values("snowfall_in")

    parts = [
        f"temp {min(temps):.0f}-{max(temps):.0f}F" if temps else "temp n/a",
        f"precip {sum(precip):.2f}in" if precip else "precip n/a",
        f"(max {max(probs):.0f}%)" if probs else "(max n/a)",
        f"wind max {max(winds):.0f}mph" if winds else "wind n/a",
        f"gusts {max(gusts):.0f}mph" if gusts else "gusts n/a",
        f"snow {sum(snow):.1f}in" if snow else "snow n/a",
        (
            f"daylight {window.sunrise:%H:%M}-{window.sunset:%H:%M}"
            if window
            else "daylight n/a"
        ),
    ]
    return "  ".join(parts)


def _alert_lines(alerts: tuple[Alert, ...] | None) -> list[str]:
    if alerts is None:
        return ["  Alerts: unknown (NWS check failed)"]
    if not alerts:
        return ["  Alerts: none"]
    lines = [f"  Alerts: {len(alerts)}"]
    for alert in alerts:
        ends = f"{alert.ends:%Y-%m-%d %H:%M %z}" if alert.ends else "n/a"
        lines.append(
            f"    - {alert.event} ({alert.severity or 'severity n/a'}), ends {ends}"
        )
    return lines


def _error_line(error: SpotError) -> str:
    status = f" (status {error.status_code})" if error.status_code is not None else ""
    return f"ERROR {error.spot_id} [{error.source}] {error.message}{status}"
