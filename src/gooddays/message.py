"""generate_message(): turn good days into one short SMS digest.

Code decides what is mentioned (only `good` entries for today and tomorrow);
the AI only words the message from the facts it is given. Everything here is
pure except the injected `writer`. If the writer fails, or its text fails
validation, a code-built fallback text is used so a good day is never lost.
"""

import json
import logging
import math
import re
import unicodedata
from collections import Counter
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

# Two SMS segments: one segment holds 160 plain characters, but a message split into
# several parts carries 153 per part (2 x 153 = 306).
MESSAGE_CAP = 306
# Below this even the one-line summary plus the longest footer might not fit.
MIN_CAP = 80

SNOW_ACTIVITIES: frozenset[Activity] = frozenset(
    {"downhill_skiing", "cross_country_skiing"}
)
ACTIVITY_NAMES: dict[Activity, str] = {
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
            if spot.id not in by_spot:
                raise ValueError(f"no conditions for scored spot {spot.id!r}")
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
    daylight = (
        hours_between(conditions, window.sunrise, window.sunset) if window else []
    )
    # Temperature and wind use the hours the scorer judged: daylight, minus hours
    # already over. Snow depth is judged over the whole daylight window.
    hours = not_yet_past(conditions, daylight)
    temperatures = _present(hours, lambda h: h.temperature_f)
    winds = _present(hours, lambda h: h.wind_speed_mph)
    depths = _present(daylight, lambda h: h.snow_depth_in)
    return GoodEntry(
        spot_id=spot.id,
        spot_name=_clean(spot.name) or spot.id,
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
            _clean(n.removeprefix("alert: ")) for n in notes if n.startswith("alert: ")
        ),
    )


def _present(
    hours: list[HourlyConditions], get: Callable[[HourlyConditions], float | None]
) -> list[float]:
    return [v for h in hours if (v := get(h)) is not None]


# Characters that are not plain GSM-7 text (they cost two characters or switch the
# whole SMS to a shorter encoding).
_UNSAFE_CHARACTERS = frozenset("^{}\\[]~|`")


def _clean(text: str) -> str:
    """Plain, SMS-safe ASCII: accents dropped, odd characters removed, one line."""
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    kept = "".join(
        c if c not in _UNSAFE_CHARACTERS and (c.isprintable() or c in "\n\t") else " "
        for c in ascii_text
    )
    return " ".join(kept.split())


def _round(value: float) -> int:
    """Round half up (Python's round() rounds halves to even: 2.5 -> 2)."""
    return math.floor(value + 0.5)


# --- Rendering facts as short strings (what both the AI and the fallback use) ---


def day_text(entry: GoodEntry) -> str:
    day = entry.day
    return f"{_WEEKDAYS[day.weekday()]} {_MONTHS[day.month - 1]} {day.day}"


def temperature_text(entry: GoodEntry) -> str | None:
    if entry.temperature_low_f is None or entry.temperature_high_f is None:
        return None
    low, high = _round(entry.temperature_low_f), _round(entry.temperature_high_f)
    if low == high:
        return f"{low} F"
    # "-5-3 F" is unreadable, so a range that starts below zero uses words.
    return f"{low} to {high} F" if low < 0 else f"{low}-{high} F"


def wind_text(entry: GoodEntry) -> str | None:
    return (
        None if entry.wind_max_mph is None else f"wind {_round(entry.wind_max_mph)} mph"
    )


def snow_text(entry: GoodEntry) -> str | None:
    if entry.snow_depth_in is None or _round(entry.snow_depth_in) <= 0:
        return None  # "0 in snow" would only confuse
    return f"{_round(entry.snow_depth_in)} in snow"


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

# A number, with a minus sign only when it is a sign (not the dash in "20-28").
_NUMBER = re.compile(r"(?<!\w)-?\d+|\d+")


def validate_text(
    text: str, facts: MessageFacts, spots: Sequence[Spot], max_chars: int
) -> list[str]:
    """What is wrong with `text` (an empty list means it is acceptable).

    Known limits: numbers are checked against the set of numbers in the facts, so
    a real number attached to the wrong fact (a wind speed that is really a
    temperature) passes, and so does a place name that is not in the config.
    """
    if not text.strip():
        return ["empty"]
    problems: list[str] = []
    if not text.isascii():
        problems.append("not_ascii")
    if any(
        c in _UNSAFE_CHARACTERS or (ord(c) < 32 and c != "\n") or ord(c) == 127
        for c in text
    ):
        problems.append("unsafe_characters")
    if len(text) > max_chars:
        problems.append("too_long")

    lowered = text.lower()
    problems.extend(_entry_problems(lowered, facts))

    # A configured spot that is not a good entry must not be named. Names of good
    # entries are removed first, so "Stowe" isn't found inside "Stowe Mountain".
    wanted = {entry.spot_name for entry in facts.entries}
    rest = lowered
    for name in sorted(wanted, key=len, reverse=True):
        rest = rest.replace(name.lower(), " ")
    for configured in spots:
        name = _clean(configured.name) or configured.id
        if name not in wanted and name.lower() in rest:
            problems.append(f"unknown_spot:{name}")

    allowed = set(_NUMBER.findall(facts_json(facts)))
    invented = sorted(set(_NUMBER.findall(text)) - allowed)
    problems.extend(f"invented_number:{number}" for number in invented)
    return problems


def _entry_problems(lowered: str, facts: MessageFacts) -> list[str]:
    """Each entry must be in its own day's group, not just somewhere in the text."""
    problems: list[str] = []
    groups = _day_groups(lowered)
    for label in ("today", "tomorrow"):
        entries = [e for e in facts.entries if e.day_label == label]
        # The label must appear exactly when some entry is on that day.
        if bool(entries) != bool(groups[label]):
            problems.append(f"day_label:{label}")
        for activity, count in Counter(e.activity for e in entries).items():
            if groups[label].count(ACTIVITY_NAMES[activity]) < count:
                problems.append(f"missing_activity:{ACTIVITY_NAMES[activity]}:{label}")
        for name, count in Counter(e.spot_name for e in entries).items():
            if groups[label].count(name.lower()) < count:
                problems.append(f"missing_entry:{name}:{label}")
    return problems


def _day_groups(lowered: str) -> dict[str, str]:
    """The text after each 'today' / 'tomorrow', up to the next one."""
    marks = [
        (m.start(), m.group()) for m in re.finditer(r"\b(today|tomorrow)\b", lowered)
    ]
    groups = {"today": "", "tomorrow": ""}
    for i, (start, label) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(lowered)
        groups[label] += lowered[start:end]
    return groups


# --- The code-built fallback ---


def build_fallback(facts: MessageFacts, max_chars: int) -> str:
    """Deterministic text. Shrinks detail, then entries, to stay within `max_chars`."""
    return _fallback_text(facts, max_chars)[0]


def _fallback_text(facts: MessageFacts, max_chars: int) -> tuple[str, int]:
    """The fallback text and how many of the entries (from the front) it names."""
    footer = footer_text(facts.unchecked_spots)
    count = len(facts.entries)
    # Detail 3 is everything; each step drops alerts, then daylight, then snow.
    for detail in (3, 2, 1, 0):
        text = _render(facts.entries, detail, 0, footer)
        if len(text) <= max_chars:
            return text, count
    # Keep at least one entry, so the text always names something.
    for dropped in range(1, count):
        shown = count - dropped
        text = _render(facts.entries[:shown], 0, dropped, footer)
        if len(text) <= max_chars:
            return text, shown
    summary = f"{count} good {'outing' if count == 1 else 'outings'} (too many to list)"
    # This summary names no entry, so none of them counts as sent.
    return (f"{summary}\n{footer}" if footer else summary), 0


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
    if max_chars < MIN_CAP:
        raise ValueError(f"max_chars must be at least {MIN_CAP}, got {max_chars}")
    facts = build_facts(spots, scores, conditions, errors, timezone)
    if not facts.entries:
        return None  # nothing good: no message and, importantly, no AI call
    return message_from_facts(facts, spots, writer, max_chars)


def message_from_facts(
    facts: MessageFacts,
    spots: Sequence[Spot],
    writer: MessageWriter | None = None,
    max_chars: int = MESSAGE_CAP,
) -> Message:
    """The digest for these facts (which must have at least one entry).

    The AI is skipped when the entries cannot all fit even in the shortest form,
    since it could not list them all either. `Message.covered` says which entries
    the text names.
    """
    if max_chars < MIN_CAP:
        raise ValueError(f"max_chars must be at least {MIN_CAP}, got {max_chars}")
    if not facts.entries:
        raise ValueError("message_from_facts needs at least one entry")

    footer = footer_text(facts.unchecked_spots)
    body_limit = max_chars - (len(footer) + 1 if footer else 0)
    shortest = _render(facts.entries, 0, 0, footer)

    if writer is None:
        return _fallback(facts, max_chars, "writer_disabled", warn=False)
    if len(shortest) > max_chars:
        return _fallback(facts, max_chars, "too_many_entries", warn=False)
    try:
        text = writer(facts_json(facts), body_limit)
    except WriterError as e:
        return _fallback(facts, max_chars, e.reason)
    except Exception as e:  # a broken writer must not lose a good day
        return _fallback(
            facts, max_chars, f"writer_error:{type(e).__name__}", error=True
        )
    if not isinstance(text, str):
        return _fallback(facts, max_chars, "writer_returned_non_text")
    text = text.strip()
    problems = validate_text(text, facts, spots, body_limit)
    if problems:
        # Only the kinds are logged (not names or numbers from the facts).
        kinds = sorted({problem.split(":")[0] for problem in problems})
        return _fallback(facts, max_chars, f"invalid:{','.join(kinds)}")
    return Message(
        body=f"{text}\n{footer}" if footer else text,
        source="ai",
        covered=tuple(
            entry.key for entry in facts.entries
        ),  # validation named every one
    )


def _fallback(
    facts: MessageFacts,
    max_chars: int,
    reason: str,
    *,
    warn: bool = True,
    error: bool = False,
) -> Message:
    log = logger.warning if warn else logger.info
    log(
        "message_fallback reason=%r entries=%d",
        reason,
        len(facts.entries),
        exc_info=error,  # an unexpected writer exception gets its traceback
    )
    body, shown = _fallback_text(facts, max_chars)
    covered = tuple(entry.key for entry in facts.entries[:shown])
    return Message(body=body, source="fallback", covered=covered)
