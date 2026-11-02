"""score_conditions(): is each day a good day for each activity?

A pure function of the fetched conditions and the thresholds from spots.yaml.
Each spot, activity and day (today and tomorrow) gets a simple pass or fail,
with every check's value and limit so the reasons can be shown.

Time comparisons go through UTC on purpose: two datetimes in the same ZoneInfo
are compared and subtracted by wall-clock time, which is wrong around a DST
change.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import assert_never
from zoneinfo import ZoneInfo

from gooddays.models import (
    Activity,
    ActivityScore,
    Alert,
    CheckResult,
    CrossCountrySkiingThresholds,
    DaylightWindow,
    DownhillSkiingThresholds,
    HikingThresholds,
    HourlyConditions,
    ScoreResult,
    SpotConditions,
    SpotScore,
    Thresholds,
)

DAYS_SCORED = 2  # today and tomorrow
HOUR = timedelta(hours=1)
NO_WINDOW_REASON = "no data: no daylight window"
SEVERE_SEVERITIES = frozenset({"severe", "extreme"})
ALERT_LIMIT = "known, with no Severe or Extreme alert"


@dataclass(frozen=True, slots=True)
class _Rule:
    """One hourly check. A missing value never passes."""

    name: str
    get: Callable[[HourlyConditions], float | None]
    ok: Callable[[float], bool]
    limit: str


def score_conditions(
    results: Sequence[SpotConditions], thresholds: Thresholds, timezone: str
) -> ScoreResult:
    """Score every spot. A spot with no forecast isn't in `results`: no score."""
    tz = ZoneInfo(timezone)
    return ScoreResult(
        results=tuple(_score_spot(conditions, thresholds, tz) for conditions in results)
    )


def _score_spot(
    conditions: SpotConditions, thresholds: Thresholds, tz: ZoneInfo
) -> SpotScore:
    today = conditions.fetched_at.astimezone(tz).date()
    days = [today + timedelta(days=n) for n in range(DAYS_SCORED)]
    scores: list[ActivityScore] = []
    for activity in conditions.spot.activities:
        for day in days:
            if activity == "hiking":
                scores.append(_score_hiking(conditions, day, thresholds.hiking, tz))
            elif activity == "downhill_skiing":
                scores.append(
                    _score_downhill_skiing(
                        conditions, day, thresholds.downhill_skiing, tz
                    )
                )
            elif activity == "cross_country_skiing":
                scores.append(
                    _score_cross_country_skiing(
                        conditions, day, thresholds.cross_country_skiing, tz
                    )
                )
            elif activity == "biking":
                scores.append(
                    ActivityScore(activity, day, "no_rules", (), (), ("no rules yet",))
                )
            else:
                assert_never(activity)  # a new Activity needs a branch above
    return SpotScore(conditions.spot, conditions.fetched_at, tuple(scores))


def _score_hiking(
    conditions: SpotConditions, day: date, t: HikingThresholds, tz: ZoneInfo
) -> ActivityScore:
    rules = [
        _Rule(
            "temperature_f",
            lambda h: h.temperature_f,
            lambda v: v >= t.min_temperature_f,
            f">= {t.min_temperature_f:g} F",
        ),
        _Rule(
            "precipitation_probability_pct",
            lambda h: h.precipitation_probability_pct,
            lambda v: v <= t.max_precipitation_probability_pct,
            f"<= {t.max_precipitation_probability_pct:g} %",
        ),
        _Rule(
            "precipitation_in",
            lambda h: h.precipitation_in,
            lambda v: v <= t.max_precipitation_in,
            f"<= {t.max_precipitation_in:g} in per hour",
        ),
    ]
    return _score_daylight_run("hiking", conditions, day, tz, rules, t.min_run_hours)


def _score_cross_country_skiing(
    conditions: SpotConditions,
    day: date,
    t: CrossCountrySkiingThresholds,
    tz: ZoneInfo,
) -> ActivityScore:
    # Depth on the ground matters here, so it is an hourly rule (unlike downhill's
    # "any snow" check), and fresh snow overnight is not required.
    rules = [
        _Rule(
            "snow_depth_in",
            lambda h: h.snow_depth_in,
            lambda v: v >= t.min_snow_depth_in,
            f">= {t.min_snow_depth_in:g} in",
        ),
        _Rule(
            "temperature_f",
            lambda h: h.temperature_f,
            lambda v: t.min_temperature_f <= v < t.max_temperature_f,
            f">= {t.min_temperature_f:g} and < {t.max_temperature_f:g} F",
        ),
        _Rule(
            "wind_speed_mph",
            lambda h: h.wind_speed_mph,
            lambda v: v < t.max_wind_mph,
            f"< {t.max_wind_mph:g} mph",
        ),
        _Rule(
            "precipitation_probability_pct",
            lambda h: h.precipitation_probability_pct,
            lambda v: v <= t.max_precipitation_probability_pct,
            f"<= {t.max_precipitation_probability_pct:g} %",
        ),
        _Rule(
            "rain_in",
            lambda h: h.rain_in,
            lambda v: v <= t.max_rain_in,
            f"<= {t.max_rain_in:g} in per hour",
        ),
    ]
    return _score_daylight_run(
        "cross_country_skiing", conditions, day, tz, rules, t.min_run_hours
    )


def _score_daylight_run(
    activity: Activity,
    conditions: SpotConditions,
    day: date,
    tz: ZoneInfo,
    rules: list[_Rule],
    min_run_hours: int,
) -> ActivityScore:
    """Score an activity that only needs good daylight hours (no overnight check)."""
    window = _daylight(conditions, day, tz)
    checks: list[CheckResult] = []
    early: list[str] = []
    if window is None:
        early.append(NO_WINDOW_REASON)
        alert_start = _midnight(day, tz)
    else:
        checks.extend(
            _hourly_checks(
                conditions, window.sunrise, window.sunset, rules, min_run_hours
            )
        )
        alert_start = window.sunrise
    return _finish(activity, conditions, day, checks, early, alert_start)


def _score_downhill_skiing(
    conditions: SpotConditions, day: date, t: DownhillSkiingThresholds, tz: ZoneInfo
) -> ActivityScore:
    window = _daylight(conditions, day, tz)
    rules = [
        _Rule(
            "temperature_f",
            lambda h: h.temperature_f,
            lambda v: v < t.max_temperature_f,
            f"< {t.max_temperature_f:g} F",
        ),
        _Rule(
            "wind_speed_mph",
            lambda h: h.wind_speed_mph,
            lambda v: v < t.max_wind_mph,
            f"< {t.max_wind_mph:g} mph",
        ),
    ]
    checks: list[CheckResult] = []
    early: list[str] = []
    # The night leading into the day: from the previous sunset up to this day's
    # sunrise. Today has no earlier day in the data, so it starts at the first row.
    previous = _daylight(conditions, day - timedelta(days=1), tz)
    is_today = day == conditions.fetched_at.astimezone(tz).date()
    if window is None:
        early.append(NO_WINDOW_REASON)
        alert_start = _midnight(day, tz)
    elif previous is None and not (is_today and conditions.hourly):
        # We can't tell where the night starts, so we can't judge the snow.
        early.append("no data: no previous sunset to start the overnight window")
        alert_start = _midnight(day, tz)
    else:
        overnight_start = (
            previous.sunset if previous is not None else conditions.hourly[0].time
        )
        checks.extend(
            _hourly_checks(
                conditions, window.sunrise, window.sunset, rules, t.min_run_hours
            )
        )
        checks.append(_snow_check(conditions, window, overnight_start, t))
        alert_start = overnight_start
    return _finish("downhill_skiing", conditions, day, checks, early, alert_start)


def _finish(
    activity: Activity,
    conditions: SpotConditions,
    day: date,
    checks: list[CheckResult],
    early_reasons: list[str],
    alert_start: datetime,
) -> ActivityScore:
    alert_check, notes = _alert_check(conditions.alerts, alert_start)
    checks.append(alert_check)
    reasons = tuple(early_reasons) + tuple(
        f"{c.name}: {c.value} (needs {c.limit})" for c in checks if not c.passed
    )
    return ActivityScore(
        activity=activity,
        day=day,
        status="good" if not reasons else "not_good",
        checks=tuple(checks),
        reasons=reasons,
        notes=notes,
    )


def _daylight(
    conditions: SpotConditions, day: date, tz: ZoneInfo
) -> DaylightWindow | None:
    # Match on the sunrise's own local date, not DaylightWindow.date, which is a
    # label at one fixed UTC offset.
    for window in conditions.daylight:
        if window.sunrise.astimezone(tz).date() == day:
            return window
    return None


def _midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tz)


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def _hourly_checks(
    conditions: SpotConditions,
    start: datetime,
    end: datetime,
    rules: list[_Rule],
    min_run_hours: int,
) -> list[CheckResult]:
    in_window = [
        h for h in conditions.hourly if _utc(start) <= _utc(h.time) < _utc(end)
    ]
    if not in_window:
        return [
            CheckResult("hourly_data", False, "no data", "hourly rows in the window")
        ]

    # The hour that contains the fetch time still counts; earlier ones are over.
    usable = [h for h in in_window if _utc(h.time) + HOUR > _utc(conditions.fetched_at)]
    if len(usable) < min_run_hours and len(usable) < len(in_window):
        return [
            CheckResult(
                "hours_left",
                False,
                f"not enough hours left ({len(usable)} h)",
                f">= {min_run_hours} h",
            )
        ]

    checks: list[CheckResult] = []
    passing_all = [True] * len(usable)
    for rule in rules:
        flags = [_passes(rule, h) for h in usable]
        passing_all = [a and b for a, b in zip(passing_all, flags, strict=True)]
        run = _longest_run(usable, flags)
        checks.append(
            CheckResult(
                rule.name,
                run >= min_run_hours,
                _describe(rule, usable, flags, run),
                rule.limit,
            )
        )
    run_all = _longest_run(usable, passing_all)
    checks.append(
        CheckResult(
            "usable_run",
            run_all >= min_run_hours,
            f"longest passing run {run_all} h of {len(usable)} usable h",
            f">= {min_run_hours} h in a row",
        )
    )
    return checks


def _passes(rule: _Rule, hour: HourlyConditions) -> bool:
    value = rule.get(hour)
    return value is not None and rule.ok(value)


def _longest_run(hours: list[HourlyConditions], flags: list[bool]) -> int:
    """The longest stretch of consecutive (one hour apart) passing hours."""
    best = current = 0
    previous: datetime | None = None
    for hour, ok in zip(hours, flags, strict=True):
        if not ok:
            current, previous = 0, None
            continue
        adjacent = previous is not None and _utc(hour.time) - previous == HOUR
        current = current + 1 if adjacent else 1
        best = max(best, current)
        previous = _utc(hour.time)
    return best


def _describe(
    rule: _Rule, hours: list[HourlyConditions], flags: list[bool], run: int
) -> str:
    text = f"{sum(flags)}/{len(hours)} hours pass, longest run {run} h"
    if all(rule.get(hour) is None for hour in hours):
        # e.g. an older saved response that never had this reading
        return f"{text}; no values for this reading in the data"
    for hour, ok in zip(hours, flags, strict=True):
        if not ok:
            value = rule.get(hour)
            shown = "missing" if value is None else f"{value:g}"
            return f"{text}; first fail {hour.time:%H:%M %z}: {shown}"
    return text


def _snow_check(
    conditions: SpotConditions,
    window: DaylightWindow,
    overnight_start: datetime,
    t: DownhillSkiingThresholds,
) -> CheckResult:
    """Snow is required: it snowed overnight, or there is snow on the ground."""
    overnight = [
        h
        for h in conditions.hourly
        if _utc(overnight_start) <= _utc(h.time) < _utc(window.sunrise)
    ]
    daytime = [
        h
        for h in conditions.hourly
        if _utc(window.sunrise) <= _utc(h.time) < _utc(window.sunset)
    ]
    snowfall = [h.snowfall_in for h in overnight if h.snowfall_in is not None]
    depth = [h.snow_depth_in for h in daytime if h.snow_depth_in is not None]
    limit = (
        f"overnight snowfall total > {t.min_snowfall_in:g} in, "
        f"or snow depth > {t.min_snow_depth_in:g} in"
    )

    snowed = bool(snowfall) and sum(snowfall) > t.min_snowfall_in
    on_ground = any(v > t.min_snow_depth_in for v in depth)
    if snowed or on_ground:
        found = []
        if snowfall and sum(snowfall) > 0:
            found.append(f"overnight snowfall {sum(snowfall):.2f} in")
        if depth and max(depth) > 0:
            found.append(f"snow depth up to {max(depth):.1f} in")
        return CheckResult("snow", True, "; ".join(found), limit)
    if not snowfall and not depth:
        return CheckResult("snow", False, "no data", limit)
    return CheckResult("snow", False, "no snow overnight and none on the ground", limit)


def _alert_check(
    alerts: tuple[Alert, ...] | None, window_start: datetime
) -> tuple[CheckResult, tuple[str, ...]]:
    """Unknown alerts never pass; Severe/Extreme alerts still active fail."""
    if alerts is None:
        return CheckResult("alerts", False, "unknown", ALERT_LIMIT), ()
    # `Alert` has no start time, only `ends`: one that ends at or before the
    # window start is over; anything else is treated as active.
    active = [a for a in alerts if a.ends is None or _utc(a.ends) > _utc(window_start)]
    severe = [a for a in active if (a.severity or "").lower() in SEVERE_SEVERITIES]
    others = tuple(f"alert: {_label(a)}" for a in active if a not in severe)
    if severe:
        return (
            CheckResult(
                "alerts", False, ", ".join(_label(a) for a in severe), ALERT_LIMIT
            ),
            others,
        )
    return CheckResult("alerts", True, "none severe", ALERT_LIMIT), others


def _label(alert: Alert) -> str:
    return f"{alert.event} ({alert.severity or 'severity n/a'})"
