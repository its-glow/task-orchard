"""Turning text into instants and instants into what a small model can use.

SPEC section 3 is the contract. The grammar is deliberately small and fixed
(3.2) so that the model never works out a calendar date itself: it passes
"friday" and the server knows which Friday.

- parse_when(text, tz, now, day_means): text -> aware UTC datetime.
- stamp(instant, tz, now, deadline=False): {"at", "day", "rel"}.
- now_stamp(now, tz): the stamp every response carries, without `rel`.
- relative(instant, now, deadline=False): the `rel` phrase of 3.3.
- day_snooze_end(start, days, tz): where a snooze counted in days ends (3.4).
- to_utc(naive_local, tz): wall-clock time to an instant, with the
  daylight-saving rules of 3.2.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from task_orchard.core.errors import Invalid

PARSE_HINT = (
    "Use a date like 2026-11-20, a date and time like 2026-11-20T17:00, "
    'a weekday name, "tomorrow", or an offset like +3d.'
)

# Fixed English names, never strftime("%a"), which follows the process locale.
DAY_ABBREVIATIONS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_WEEKDAYS: dict[str, int] = {}
for _index, _names in enumerate(
    (
        ("monday", "mon"),
        ("tuesday", "tue", "tues"),
        ("wednesday", "wed"),
        ("thursday", "thu", "thur", "thurs"),
        ("friday", "fri"),
        ("saturday", "sat"),
        ("sunday", "sun"),
    )
):
    for _name in _names:
        _WEEKDAYS[_name] = _index

_TIME = r"(?P<hour>\d{1,2}):(?P<minute>\d{2})"
_ISO_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:z|[+-]\d{2}:?\d{2})"
)
_ISO_LOCAL = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})[t ]" + _TIME + r"(?::\d{2}(?:\.\d+)?)?")
_EXACT_OFFSET = re.compile(r"\+(?P<n>\d{1,6})(?P<unit>[mh])")
_DAY_THEN_TIME = re.compile(r"(?P<day>\S+)(?:\s+" + _TIME + r")?")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DAY_OFFSET = re.compile(r"\+(?P<n>\d{1,5})(?P<unit>[dw])")


class DayMeans(StrEnum):
    """What a day given without a time stands for (SPEC 3.2)."""

    DEADLINE = "deadline"  # the end of the day, 23:59
    SNOOZE = "snooze"  # the start of the day, 00:00
    COMPLETION = "completion"  # midday, or now if midday is still to come


def _reject() -> Invalid:
    return Invalid("That is not a time this server understands.", hint=PARSE_HINT)


def to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    """Local wall-clock time to an aware UTC instant.

    A time that does not exist (the hour skipped when clocks go forward) moves
    forward to the next valid minute. An ambiguous one (the hour repeated when
    clocks go back) takes its first occurrence.
    """
    naive = naive.replace(second=0, microsecond=0, tzinfo=None)
    candidate = naive
    # A gap is at most a few hours; step through it a minute at a time.
    for _ in range(24 * 60):
        aware = candidate.replace(tzinfo=tz, fold=0)
        instant = aware.astimezone(UTC)
        if instant.astimezone(tz).replace(tzinfo=None) == candidate:
            return instant
        candidate += timedelta(minutes=1)
    raise AssertionError(f"no valid local time after {naive} in {tz}")  # pragma: no cover


def local_date(instant: datetime, tz: ZoneInfo) -> date:
    return instant.astimezone(tz).date()


def _resolve_day(day: date, tz: ZoneInfo, now: datetime, means: DayMeans) -> datetime:
    if means is DayMeans.DEADLINE:
        return to_utc(datetime.combine(day, time(23, 59)), tz)
    if means is DayMeans.SNOOZE:
        return to_utc(datetime.combine(day, time(0, 0)), tz)
    return min(to_utc(datetime.combine(day, time(12, 0)), tz), now)


def _parse_day(word: str, tz: ZoneInfo, now: datetime) -> date | None:
    today = local_date(now, tz)
    if word == "today":
        return today
    if word == "tomorrow":
        return today + timedelta(days=1)
    if word in _WEEKDAYS:
        ahead = (_WEEKDAYS[word] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    if _DATE.fullmatch(word):
        try:
            return date.fromisoformat(word)
        except ValueError:
            raise _reject() from None
    if match := _DAY_OFFSET.fullmatch(word):
        days = int(match["n"]) * (7 if match["unit"] == "w" else 1)
        try:
            return today + timedelta(days=days)
        except OverflowError:
            raise _reject() from None
    return None


def parse_when(text: str, tz: ZoneInfo, now: datetime, day_means: DayMeans) -> datetime:
    """Parse one of the forms of SPEC 3.2 into an aware UTC datetime."""
    if not isinstance(text, str):
        raise _reject()
    cleaned = " ".join(text.strip().lower().split())
    if not cleaned:
        raise _reject()

    if _ISO_INSTANT.fullmatch(cleaned):
        try:
            return (
                datetime.fromisoformat(cleaned.upper())
                .astimezone(UTC)
                .replace(second=0, microsecond=0)
            )
        except ValueError:
            raise _reject() from None

    if match := _EXACT_OFFSET.fullmatch(cleaned):
        unit = "minutes" if match["unit"] == "m" else "hours"
        return now + timedelta(**{unit: int(match["n"])})

    if match := _ISO_LOCAL.fullmatch(cleaned):
        day_text, hour, minute = match["date"], match["hour"], match["minute"]
    elif match := _DAY_THEN_TIME.fullmatch(cleaned):
        day_text, hour, minute = match["day"], match["hour"], match["minute"]
    else:
        raise _reject()

    day = _parse_day(day_text, tz, now)
    if day is None:
        raise _reject()
    if hour is None:
        return _resolve_day(day, tz, now, day_means)
    h, m = int(hour), int(minute)
    if h > 23 or m > 59:
        raise _reject()
    return to_utc(datetime.combine(day, time(h, m)), tz)


def relative(instant: datetime, now: datetime, *, deadline: bool = False) -> str:
    """The `rel` phrase of SPEC 3.3. Counts are rounded down."""
    seconds = (instant - now).total_seconds()
    future = seconds > 0
    distance = abs(seconds)
    if distance < 60:
        return "now"
    if distance < 3600:
        amount = f"{int(distance // 60)} min"
    elif distance < 48 * 3600:
        hours = int(distance // 3600)
        amount = f"{hours} hour" if hours == 1 else f"{hours} hours"
    else:
        amount = f"{int(distance // 86400)} days"
    if future:
        return f"in {amount}"
    if deadline:
        return f"overdue by {amount}"
    return f"{amount} ago"


def _at(instant: datetime, tz: ZoneInfo) -> tuple[str, str]:
    local = instant.astimezone(tz)
    return local.strftime("%Y-%m-%dT%H:%M"), DAY_ABBREVIATIONS[local.weekday()]


def at_text(instant: datetime, tz: ZoneInfo) -> str:
    """Just the `at` part of a stamp: local wall-clock time to the minute."""
    return _at(instant, tz)[0]


def stamp(
    instant: datetime, tz: ZoneInfo, now: datetime, *, deadline: bool = False
) -> dict[str, str]:
    at, day = _at(instant, tz)
    return {"at": at, "day": day, "rel": relative(instant, now, deadline=deadline)}


def now_stamp(now: datetime, tz: ZoneInfo) -> dict[str, str]:
    at, day = _at(now, tz)
    return {"at": at, "day": day}


def day_snooze_end(start: datetime, days: int, tz: ZoneInfo) -> datetime:
    """Local midnight at the start of the day `days` x 24 hours after `start`."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise Invalid(
            "A number of days must be a whole number, 1 or more.",
            hint="Pass days as an integer such as 1 or 3, or give a date instead.",
        )
    target = local_date(start + timedelta(days=days), tz)
    return to_utc(datetime.combine(target, time(0, 0)), tz)
