"""generate_message(): turn good days into one short SMS digest.

Code decides what is mentioned (only `good` entries for today and tomorrow);
the AI only words the message from the facts it is given. Everything here is
pure except the injected `writer`. If the writer fails, or its text fails
validation, a code-built fallback text is used so a good day is never lost.
"""

import json
import logging
import re
import unicodedata
from collections.abc import Callable, Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from gooddays.models import (
    Activity,
    GoodEntry,
    HourlyConditions,
    Message,
    MessageFacts,
    ScoreResult,
    Spot,
    SpotConditions,
    SpotError,
)
from gooddays.score import daylight_window, hours_between, not_yet_past

logger = logging.getLogger(__name__)

# About two SMS segments of plain ASCII.
MESSAGE_CAP = 320

SNOW_ACTIVITIES: frozenset[str] = frozenset({"downhill_skiing", "cross_country_skiing"})
ACTIVITY_NAMES: dict[str, str] = {
    "hiking": "hiking",
    "biking": "biking",
    "downhill_skiing": "downhill skiing",
    "cross_country_skiing": "cross-country skiing",
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# (facts as JSON, maximum characters) -> message text. Raises WriterError.
MessageWriter = Callable[[str, int], str]


class WriterError(Exception):
    """The writer could not produce text. `reason` is a short key for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- Choosing entries and building facts ---


def build_facts(
    spots: Sequence[Spot],
    scores: ScoreResult,
    conditions: Sequence[SpotConditions],
    errors: Sequence[SpotError],
    timezone: str,
) -> MessageFacts:
    """The good entries (today first, then config order) plus the unchecked count."""
    tz = ZoneInfo(timezone)
    by_spot = {c.spot.id: c for c in conditions}
    spot_order = {spot.id: i for i, spot in enumerate(spots)}

    ordered: list[tuple[date, int, int, GoodEntry]] = []
    for spot_score in scores.results:
        spot = spot_score.spot
        today = spot_score.fetched_at.astimezone(tz).date()
        for score in spot_score.scores:
            if score.status != "good":
                continue
            entry = _entry(
                by_spot[spot.id],
                spot,
                score.activity,
                score.day,
                today,
                score.notes,
                tz,
            )
            activity_index = spot.activities.index(score.activity)
            ordered.append(
                (score.day, spot_order.get(spot.id, len(spots)), activity_index, entry)
            )
    ordered.sort(key=lambda item: item[:3])

    return MessageFacts(
        entries=tuple(entry for *_, entry in ordered),
        unchecked_spots=len({error.spot_id for error in errors}),
    )


def _entry(
    conditions: SpotConditions,
    spot: Spot,
    activity: Activity,
    day: date,
    today: date,
    notes: tuple[str, ...],
    tz: ZoneInfo,
) -> GoodEntry:
    window = daylight_window(conditions, day, tz)
    # The same hours the scorer judged: daylight, minus hours already over.
    hours = (
        not_yet_past(
            conditions, hours_between(conditions, window.sunrise, window.sunset)
        )
        if window is not None
        else []
    )
    temperatures = _present(hours, lambda h: h.temperature_f)
    winds = _present(hours, lambda h: h.wind_speed_mph)
    depths = _present(hours, lambda h: h.snow_depth_in)
    return GoodEntry(
        spot_name=_ascii(spot.name),
        activity=activity,
        day=day,
        day_label="today" if day == today else "tomorrow",
        temperature_low_f=min(temperatures) if temperatures else None,
        temperature_high_f=max(temperatures) if temperatures else None,
        wind_max_mph=max(winds) if winds else None,
        sunrise=window.sunrise.astimezone(tz) if window else None,
        sunset=window.sunset.astimezone(tz) if window else None,
        snow_depth_in=max(depths) if depths and activity in SNOW_ACTIVITIES else None,
        alert_notes=tuple(
            _ascii(n.removeprefix("alert: ")) for n in notes if n.startswith("alert: ")
        ),
    )


def _present(
    hours: list[HourlyConditions], get: Callable[[HourlyConditions], float | None]
) -> list[float]:
    return [v for h in hours if (v := get(h)) is not None]


def _ascii(text: str) -> str:
    """Plain ASCII (accents dropped), so an SMS stays in the cheap encoding."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


# --- Rendering facts as short strings (what both the AI and the fallback use) ---


def day_text(entry: GoodEntry) -> str:
    day = entry.day
    return f"{_WEEKDAYS[day.weekday()]} {_MONTHS[day.month - 1]} {day.day}"


def temperature_text(entry: GoodEntry) -> str | None:
    if entry.temperature_low_f is None or entry.temperature_high_f is None:
        return None
    low, high = round(entry.temperature_low_f), round(entry.temperature_high_f)
    return f"{low} F" if low == high else f"{low}-{high} F"


def wind_text(entry: GoodEntry) -> str | None:
    return (
        None if entry.wind_max_mph is None else f"wind {round(entry.wind_max_mph)} mph"
    )


def snow_text(entry: GoodEntry) -> str | None:
    return (
        None if entry.snow_depth_in is None else f"{round(entry.snow_depth_in)} in snow"
    )


def daylight_text(entry: GoodEntry) -> str | None:
    if entry.sunrise is None or entry.sunset is None:
        return None
    return f"daylight {_clock(entry.sunrise)}-{_clock(entry.sunset)}"


def _clock(moment: datetime) -> str:
    return f"{moment.hour}:{moment.minute:02d}"


def footer_text(unchecked_spots: int) -> str | None:
    if unchecked_spots == 0:
        return None
    noun = "spot" if unchecked_spots == 1 else "spots"
    return f"{unchecked_spots} {noun} couldn't be checked"


def facts_json(facts: MessageFacts) -> str:
    """The facts for the AI: only display strings, so it copies and never calculates."""
    entries = [
        {
            "spot": entry.spot_name,
            "activity": ACTIVITY_NAMES[entry.activity],
            "date": day_text(entry),
            "when": entry.day_label,
            "temperature": temperature_text(entry),
            "wind": wind_text(entry),
            "snow_on_ground": snow_text(entry),
            "daylight": daylight_text(entry),
            "minor_alerts": list(entry.alert_notes),
        }
        for entry in facts.entries
    ]
    return json.dumps({"entries": entries}, indent=2)


# --- Validation ---


def validate_text(
    text: str, facts: MessageFacts, spots: Sequence[Spot], max_chars: int
) -> list[str]:
    """What is wrong with `text` (an empty list means it is acceptable)."""
    problems: list[str] = []
    if not text.strip():
        return ["empty"]
    if not text.isascii():
        problems.append("not_ascii")
    if len(text) > max_chars:
        problems.append("too_long")

    lowered = text.lower()
    wanted = {entry.spot_name for entry in facts.entries}
    problems.extend(
        f"missing_entry:{name}"
        for name in sorted(wanted)
        if name.lower() not in lowered
    )
    # A configured spot that is not a good entry must not be named. Names of good
    # entries are removed first, so "Stowe" isn't found inside "Stowe Mountain".
    rest = lowered
    for name in sorted(wanted, key=len, reverse=True):
        rest = rest.replace(name.lower(), " ")
    problems.extend(
        f"unknown_spot:{_ascii(spot.name)}"
        for spot in spots
        if _ascii(spot.name) not in wanted and _ascii(spot.name).lower() in rest
    )
    for label in ("today", "tomorrow"):
        # Each label must appear exactly when some entry is on that day.
        if any(entry.day_label == label for entry in facts.entries) != (
            label in lowered
        ):
            problems.append(f"day_label:{label}")

    allowed = set(re.findall(r"\d+", facts_json(facts)))
    invented = sorted(set(re.findall(r"\d+", text)) - allowed)
    problems.extend(f"invented_number:{number}" for number in invented)
    return problems


# --- The code-built fallback ---


def build_fallback(facts: MessageFacts, max_chars: int) -> str:
    """Deterministic text. Shrinks detail, then entries, to stay within `max_chars`."""
    footer = footer_text(facts.unchecked_spots)
    # Detail 3 is everything; each step drops alerts, then daylight, then snow.
    for detail in (3, 2, 1, 0):
        text = _render(facts.entries, detail, 0, footer)
        if len(text) <= max_chars:
            return text
    for dropped in range(1, len(facts.entries) + 1):
        text = _render(
            facts.entries[: len(facts.entries) - dropped], 0, dropped, footer
        )
        if len(text) <= max_chars:
            return text
    return f"{len(facts.entries)} good days (too many to list)"[:max_chars]


def _render(
    entries: Sequence[GoodEntry], detail: int, dropped: int, footer: str | None
) -> str:
    lines: list[str] = []
    current: date | None = None
    for entry in entries:
        if entry.day != current:
            current = entry.day
            lines.append(f"{entry.day_label.capitalize()} {day_text(entry)}:")
        parts = [temperature_text(entry), wind_text(entry)]
        if detail >= 1:
            parts.append(snow_text(entry))
        if detail >= 2:
            parts.append(daylight_text(entry))
        if detail >= 3:
            parts.extend(entry.alert_notes)
        facts = ", ".join(part for part in parts if part)
        label = f"{entry.spot_name} {ACTIVITY_NAMES[entry.activity]}"
        lines.append(f"{label}: {facts}" if facts else label)
    if dropped:
        lines.append(f"+{dropped} more")
    if footer:
        lines.append(footer)
    return "\n".join(lines)


# --- The digest ---


def generate_message(
    spots: Sequence[Spot],
    scores: ScoreResult,
    conditions: Sequence[SpotConditions],
    errors: Sequence[SpotError],
    timezone: str,
    writer: MessageWriter | None = None,
    max_chars: int = MESSAGE_CAP,
) -> Message | None:
    """One SMS digest of the good days, or None when there is nothing to send.

    With no `writer` the fallback text is used (no AI call is made).
    """
    facts = build_facts(spots, scores, conditions, errors, timezone)
    if not facts.entries:
        return None  # nothing good: no message and, importantly, no AI call

    footer = footer_text(facts.unchecked_spots)
    body_limit = max_chars - (len(footer) + 1 if footer else 0)
    reason = "writer_disabled"
    if writer is not None:
        try:
            text = writer(facts_json(facts), body_limit).strip()
        except WriterError as e:
            reason = e.reason
        except Exception as e:  # a broken writer must not lose a good day
            reason = f"writer_error:{type(e).__name__}"
        else:
            problems = validate_text(text, facts, spots, body_limit)
            if not problems:
                return Message(
                    body=f"{text}\n{footer}" if footer else text, source="ai"
                )
            reason = f"invalid:{','.join(problems)}"

    logger.info("message_fallback reason=%r entries=%d", reason, len(facts.entries))
    return Message(body=build_fallback(facts, max_chars), source="fallback")
