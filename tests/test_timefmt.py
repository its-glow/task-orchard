"""SPEC section 3: the time grammar, stamps, relative phrases and day-counted snoozes.

NOW is Wednesday 18 March 2026, 10:00 in London (GMT). The UK springs forward
at 01:00 on 29 March 2026 and falls back at 02:00 on 25 October 2026.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from task_orchard.core.errors import Invalid
from task_orchard.core.timefmt import (
    PARSE_HINT,
    DayMeans,
    day_snooze_end,
    now_stamp,
    parse_when,
    relative,
    stamp,
)
from tests.conftest import NOW, TZ


def local(y: int, mo: int, d: int, h: int = 0, mi: int = 0, fold: int = 0) -> datetime:
    """An instant given as London wall-clock time, returned in UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=TZ, fold=fold).astimezone(UTC)


def parse(text: str, means: DayMeans = DayMeans.DEADLINE, now: datetime = NOW) -> datetime:
    return parse_when(text, TZ, now, means)


# --- The forms of 3.2 ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-20", local(2026, 11, 20, 23, 59)),
        ("2026-11-20T17:00", local(2026, 11, 20, 17)),
        ("2026-11-20 17:00", local(2026, 11, 20, 17)),
        ("2026-11-20t17:00", local(2026, 11, 20, 17)),
        ("2026-11-20T17:00Z", datetime(2026, 11, 20, 17, tzinfo=UTC)),
        ("2026-11-20T17:00+01:00", datetime(2026, 11, 20, 16, tzinfo=UTC)),
        ("2026-11-20T17:00:30", local(2026, 11, 20, 17)),
        ("today", local(2026, 3, 18, 23, 59)),
        ("Today", local(2026, 3, 18, 23, 59)),
        ("  tomorrow  ", local(2026, 3, 19, 23, 59)),
        ("friday", local(2026, 3, 20, 23, 59)),
        ("fri", local(2026, 3, 20, 23, 59)),
        ("FRIDAY", local(2026, 3, 20, 23, 59)),
        ("+3d", local(2026, 3, 21, 23, 59)),
        ("+2w", local(2026, 4, 1, 23, 59)),
        ("+90m", NOW + timedelta(minutes=90)),
        ("+4h", NOW + timedelta(hours=4)),
        ("friday 17:00", local(2026, 3, 20, 17)),
        ("tomorrow 09:30", local(2026, 3, 19, 9, 30)),
        ("tomorrow 9:30", local(2026, 3, 19, 9, 30)),
        ("+3d 12:00", local(2026, 3, 21, 12)),
        ("today 08:00", local(2026, 3, 18, 8)),
    ],
)
def test_every_form_parses(text: str, expected: datetime) -> None:
    result = parse(text)
    assert result == expected
    assert result.tzinfo is UTC


def test_a_weekday_is_strictly_after_today() -> None:
    # NOW is a Wednesday: "wednesday" is a week away, even later the same day.
    assert parse("wednesday") == local(2026, 3, 25, 23, 59)
    assert parse("wed 17:00") == local(2026, 3, 25, 17)
    assert parse("thursday") == local(2026, 3, 19, 23, 59)
    assert parse("tue") == local(2026, 3, 24, 23, 59)


@pytest.mark.parametrize(
    ("means", "expected"),
    [
        (DayMeans.DEADLINE, local(2026, 3, 20, 23, 59)),
        (DayMeans.SNOOZE, local(2026, 3, 20, 0, 0)),
    ],
)
def test_a_bare_day_depends_on_the_parameter(means: DayMeans, expected: datetime) -> None:
    assert parse("friday", means) == expected
    assert parse("2026-03-20", means) == expected


def test_a_bare_day_for_a_completion_is_midday_or_now() -> None:
    assert parse("2026-03-16", DayMeans.COMPLETION) == local(2026, 3, 16, 12)
    # Midday today is still in the future at 10:00, so it means now.
    assert parse("today", DayMeans.COMPLETION) == NOW
    later = local(2026, 3, 18, 15)
    assert parse("today", DayMeans.COMPLETION, now=later) == local(2026, 3, 18, 12)


def test_a_time_overrides_what_a_bare_day_means() -> None:
    assert parse("friday 17:00", DayMeans.SNOOZE) == local(2026, 3, 20, 17)
    assert parse("2026-03-16 08:00", DayMeans.COMPLETION) == local(2026, 3, 16, 8)


def test_exact_offsets_ignore_what_a_bare_day_means() -> None:
    for means in DayMeans:
        assert parse("+4h", means) == NOW + timedelta(hours=4)
        assert parse("2026-11-20T17:00Z", means) == datetime(2026, 11, 20, 17, tzinfo=UTC)


# --- Daylight saving ---


def test_a_time_that_does_not_exist_moves_forward_to_the_next_valid_minute() -> None:
    # 29 March 2026: London jumps from 01:00 GMT to 02:00 BST.
    assert parse("2026-03-29T01:30") == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert parse("2026-03-29T01:30") == local(2026, 3, 29, 2, 0)
    assert parse("2026-03-29T00:59") == datetime(2026, 3, 29, 0, 59, tzinfo=UTC)


def test_an_ambiguous_time_takes_the_first_occurrence() -> None:
    # 25 October 2026: 01:00 to 02:00 happens twice, first in BST.
    assert parse("2026-10-25T01:30") == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_day_offsets_count_calendar_days_across_a_change() -> None:
    before = local(2026, 3, 28, 22)
    assert parse("+1d 09:00", now=before) == local(2026, 3, 29, 9)
    assert parse("+1d", DayMeans.SNOOZE, now=before) == local(2026, 3, 29, 0)


# --- Rejection ---


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "next friday",
        "in 3 days",
        "2026-02-30",
        "2026-11-20T25:00",
        "friday 25:00",
        "friday 9",
        "+3",
        "+3y",
        "-3d",
        "+90m 12:00",
        "2026-11-20T17:00Z 09:00",
        "noon",
    ],
)
def test_anything_else_is_rejected_with_the_hint(text: str) -> None:
    with pytest.raises(Invalid) as caught:
        parse(text)
    assert caught.value.hint == PARSE_HINT


def test_the_hint_is_the_one_in_the_spec() -> None:
    assert PARSE_HINT == (
        "Use a date like 2026-11-20, a date and time like 2026-11-20T17:00, "
        'a weekday name, "tomorrow", or an offset like +3d.'
    )


# --- Stamps ---


def test_a_stamp_is_local_wall_clock_time() -> None:
    assert stamp(local(2026, 3, 20, 17), TZ, NOW) == {
        "at": "2026-03-20T17:00",
        "day": "Fri",
        "rel": "in 2 days",
    }
    # Summer time: the stored instant is 16:00 UTC, the user sees 17:00.
    summer = datetime(2026, 7, 3, 16, tzinfo=UTC)
    assert stamp(summer, TZ, NOW)["at"] == "2026-07-03T17:00"


def test_the_now_stamp_has_no_relative_phrase() -> None:
    assert now_stamp(NOW, TZ) == {"at": "2026-03-18T10:00", "day": "Wed"}


def test_a_stamp_for_a_passed_deadline_says_overdue() -> None:
    assert stamp(NOW - timedelta(days=6), TZ, NOW, deadline=True)["rel"] == "overdue by 6 days"
    assert stamp(NOW + timedelta(days=6), TZ, NOW, deadline=True)["rel"] == "in 6 days"


@given(st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1)))
@settings(max_examples=200)
def test_a_stamp_parses_back_to_the_same_minute(naive: datetime) -> None:
    instant = naive.replace(tzinfo=UTC, second=0, microsecond=0)
    back = parse(stamp(instant, TZ, NOW)["at"])
    in_second_fold = (
        instant.astimezone(TZ).fold == 1
        or instant.astimezone(TZ).replace(fold=1).utcoffset()
        != instant.astimezone(TZ).replace(fold=0).utcoffset()
    )
    if not in_second_fold:
        assert back == instant


# --- Relative phrases (the table in 3.3) ---


@pytest.mark.parametrize(
    ("delta", "future", "past", "overdue"),
    [
        (timedelta(seconds=0), "now", "now", "now"),
        (timedelta(seconds=59), "now", "now", "now"),
        (timedelta(minutes=1), "in 1 min", "1 min ago", "overdue by 1 min"),
        (timedelta(minutes=59, seconds=59), "in 59 min", "59 min ago", "overdue by 59 min"),
        (timedelta(minutes=60), "in 1 hour", "1 hour ago", "overdue by 1 hour"),
        (timedelta(hours=1, minutes=59), "in 1 hour", "1 hour ago", "overdue by 1 hour"),
        (timedelta(hours=2), "in 2 hours", "2 hours ago", "overdue by 2 hours"),
        (timedelta(hours=47, minutes=59), "in 47 hours", "47 hours ago", "overdue by 47 hours"),
        (timedelta(hours=48), "in 2 days", "2 days ago", "overdue by 2 days"),
        (timedelta(days=6, hours=23), "in 6 days", "6 days ago", "overdue by 6 days"),
        (timedelta(days=400), "in 400 days", "400 days ago", "overdue by 400 days"),
    ],
)
def test_relative_phrases(delta: timedelta, future: str, past: str, overdue: str) -> None:
    assert relative(NOW + delta, NOW) == future
    assert relative(NOW - delta, NOW) == past
    assert relative(NOW - delta, NOW, deadline=True) == overdue
    assert relative(NOW + delta, NOW, deadline=True) == future


# --- Day-counted snoozes (3.4) ---


def test_a_day_counted_snooze_ends_at_local_midnight() -> None:
    monday_evening = local(2026, 3, 16, 19)
    assert day_snooze_end(monday_evening, 2, TZ) == local(2026, 3, 18, 0)
    assert day_snooze_end(local(2026, 3, 16, 0, 30), 1, TZ) == local(2026, 3, 17, 0)


def test_a_day_counted_snooze_uses_the_local_day_across_a_change() -> None:
    # Saturday 28 March 23:30 GMT plus 24 hours is Monday 00:30 BST, so the
    # snooze ends at the start of Monday, local time.
    assert day_snooze_end(local(2026, 3, 28, 23, 30), 1, TZ) == local(2026, 3, 30, 0)
    # In summer, local midnight is 23:00 UTC the evening before.
    assert day_snooze_end(local(2026, 7, 1, 12), 2, TZ) == datetime(2026, 7, 2, 23, tzinfo=UTC)


def test_day_counts_are_whole_numbers() -> None:
    with pytest.raises(Invalid):
        day_snooze_end(NOW, 0, TZ)
    with pytest.raises(Invalid):
        day_snooze_end(NOW, 1.5, TZ)  # type: ignore[arg-type]
