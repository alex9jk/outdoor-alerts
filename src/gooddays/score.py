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
from zoneinfo import ZoneInfo

from gooddays.models import (
    Activity,
    ActivityScore,
    Alert,
    CheckResult,
    DaylightWindow,
    HikingThresholds,
    HourlyConditions,
    ScoreResult,
    SkiingThresholds,
    SpotConditions,
    SpotScore,
    Thresholds,
)

DAYS_SCORED = 2  # today and tomorrow
HOUR = timedelta(hours=1)
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
            elif activity == "skiing":
                scores.append(_score_skiing(conditions, day, thresholds.skiing, tz))
            else:
                scores.append(
                    ActivityScore(activity, day, "no_rules", (), (), ("no rules yet",))
                )
    return SpotScore(conditions.spot, conditions.fetched_at, tuple(scores))


def _score_hiking(
    conditions: SpotConditions, day: date, t: HikingThresholds, tz: ZoneInfo
) -> ActivityScore:
    window = _daylight(conditions, day, tz)
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
    checks: list[CheckResult] = []
    early: list[str] = []
    if window is None:
        early.append("no data: no daylight window")
        alert_start = datetime.combine(day, time.min, tz)
    else:
        checks.extend(
            _hourly_checks(
                conditions, window.sunrise, window.sunset, rules, t.min_run_hours
            )
        )
        alert_start = window.sunrise
    return _finish("hiking", conditions, day, checks, early, alert_start)


def _score_skiing(
    conditions: SpotConditions, day: date, t: SkiingThresholds, tz: ZoneInfo
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
    if window is None:
        early.append("no data: no daylight window")
        alert_start = datetime.combine(day, time.min, tz)
    else:
        # The night leading into the day: from the previous sunset (or the start
        # of the data, for today) up to this day's sunrise.
        previous = _daylight(conditions, day - timedelta(days=1), tz)
        if previous is not None:
            overnight_start = previous.sunset
        elif conditions.hourly:
            overnight_start = conditions.hourly[0].time
        else:
            overnight_start = window.sunrise
        checks.extend(
            _hourly_checks(
                conditions, window.sunrise, window.sunset, rules, t.min_run_hours
            )
        )
        checks.append(_snow_check(conditions, window, overnight_start, t))
        alert_start = overnight_start
    return _finish("skiing", conditions, day, checks, early, alert_start)


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
    for hour, ok in zip(hours, flags, strict=True):
        if not ok:
            value = rule.get(hour)
            shown = "missing" if value is None else f"{value:g}"
            return f"{text}; first fail {hour.time:%H:%M}: {shown}"
    return text


def _snow_check(
    conditions: SpotConditions,
    window: DaylightWindow,
    overnight_start: datetime,
    t: SkiingThresholds,
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
        f"overnight snowfall > {t.min_snowfall_in:g} in, "
        f"or snow depth > {t.min_snow_depth_in:g} in"
    )

    snowed = any(v > t.min_snowfall_in for v in snowfall)
    on_ground = any(v > t.min_snow_depth_in for v in depth)
    if snowed or on_ground:
        found = []
        if snowfall:
            found.append(f"overnight snowfall {sum(snowfall):.2f} in")
        if depth:
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
