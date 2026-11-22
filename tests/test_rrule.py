"""SPEC 7.1: rule validation and next_slot. The clock is Wed 18 March 2026, 10:00 London."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from task_orchard.core import recurrence
from task_orchard.core.errors import Invalid
from task_orchard.core.service import Orchard
from task_orchard.db.models import Task
from tests.conftest import TZ


def slots(orchard: Orchard, rrule: str, **kw: object) -> list[str]:
    result = orchard.create_task("Repeat", recurrence={"mode": "scheduled", "rrule": rrule, **kw})
    return [s["at"] for s in result["next_slots"]]


def stored(orchard: Orchard, task_id: int) -> Task:
    with orchard.tx() as tx:
        task = tx.session.get(Task, task_id)
        tx.session.expunge(task)
        return task


# --- Validation ---


@pytest.mark.parametrize(
    "rrule",
    [
        "FREQ=HOURLY",
        "FREQ=MINUTELY;INTERVAL=30",
        "FREQ=DAILY;BYHOUR=9,17",
        "FREQ=DAILY;BYMINUTE=30",
        "FREQ=DAILY;BYSECOND=5",
        "DTSTART:20260101T000000\nRRULE:FREQ=DAILY",
        "FREQ=WEEKLY;DTSTART=20260101",
        "BYDAY=FR",
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ=WEEKLY;UNTIL=20200101",  # no slots after now
        "nonsense",
        "",
    ],
)
def test_invalid_rules_are_rejected(orchard: Orchard, rrule: str) -> None:
    with pytest.raises(Invalid) as caught:
        orchard.create_task("Repeat", recurrence={"mode": "scheduled", "rrule": rrule})
    assert caught.value.hint


def test_time_parts_point_at_due_time(orchard: Orchard) -> None:
    with pytest.raises(Invalid) as caught:
        slots(orchard, "FREQ=DAILY;BYHOUR=9,17")
    assert "due_time" in caught.value.hint


def test_a_leading_rrule_prefix_is_stripped(orchard: Orchard) -> None:
    assert slots(orchard, "RRULE:freq=weekly;byday=fr", due_time="17:00")[0] == "2026-03-20T17:00"


def test_until_in_utc_is_read_as_local_time() -> None:
    # 08:00Z on 1 April is 09:00 in London (BST), so the 09:00 slot that day counts.
    rule = recurrence.normalise_rrule("FREQ=DAILY;UNTIL=20260401T080000Z", TZ)
    assert rule == "FREQ=DAILY;UNTIL=20260401T090000"
    task = Task(rrule=rule, recur_dtstart=datetime(2026, 3, 18, 9, 0))
    last = recurrence.next_slot(task, datetime(2026, 3, 31, 9, 0, tzinfo=UTC), TZ)
    assert last == datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
    assert recurrence.next_slot(task, last, TZ) is None


def test_until_as_a_date_is_accepted(orchard: Orchard) -> None:
    assert slots(orchard, "FREQ=DAILY;UNTIL=20260320", due_time="09:00") == [
        "2026-03-19T09:00",
        "2026-03-20T09:00",
    ]


# --- next_slot ---


def test_weekly(orchard: Orchard) -> None:
    assert slots(orchard, "FREQ=WEEKLY;BYDAY=FR", due_time="17:00") == [
        "2026-03-20T17:00",
        "2026-03-27T17:00",
        "2026-04-03T17:00",  # after the clocks change on 29 March: still 17:00 local
    ]


def test_local_time_holds_across_daylight_saving() -> None:
    task = Task(rrule="FREQ=WEEKLY;BYDAY=FR", recur_dtstart=datetime(2026, 3, 18, 17, 0))
    before = recurrence.next_slot(task, datetime(2026, 3, 21, tzinfo=UTC), TZ)
    after = recurrence.next_slot(task, before, TZ)
    assert before == datetime(2026, 3, 27, 17, 0, tzinfo=UTC)  # GMT
    assert after == datetime(2026, 4, 3, 16, 0, tzinfo=UTC)  # BST: 17:00 local


def test_monthly(orchard: Orchard) -> None:
    assert slots(orchard, "FREQ=MONTHLY;BYMONTHDAY=1", due_time="09:00") == [
        "2026-04-01T09:00",
        "2026-05-01T09:00",
        "2026-06-01T09:00",
    ]


def test_month_end(orchard: Orchard) -> None:
    assert slots(orchard, "FREQ=MONTHLY;BYMONTHDAY=-1") == [
        "2026-03-31T23:59",
        "2026-04-30T23:59",
        "2026-05-31T23:59",
    ]


def test_the_31st_skips_short_months(orchard: Orchard) -> None:
    assert slots(orchard, "FREQ=MONTHLY;BYMONTHDAY=31") == [
        "2026-03-31T23:59",
        "2026-05-31T23:59",
        "2026-07-31T23:59",
    ]


def test_yearly(orchard: Orchard) -> None:
    assert slots(orchard, "FREQ=YEARLY;BYMONTH=6;BYMONTHDAY=1") == [
        "2026-06-01T23:59",
        "2027-06-01T23:59",
        "2028-06-01T23:59",
    ]


def test_a_plain_daily_rule_repeats_from_today(orchard: Orchard) -> None:
    # Today's 09:00 has passed, so the first slot is tomorrow's.
    assert slots(orchard, "FREQ=DAILY", due_time="09:00")[0] == "2026-03-19T09:00"
    assert slots(orchard, "FREQ=DAILY", due_time="12:00")[0] == "2026-03-18T12:00"


def test_dtstart_is_naive_local_at_the_due_time(orchard: Orchard) -> None:
    task_id = orchard.create_task(
        "Repeat", recurrence={"mode": "scheduled", "rrule": "FREQ=DAILY", "due_time": "7:05"}
    )["task"]["id"]
    task = stored(orchard, task_id)
    assert task.recur_dtstart == datetime(2026, 3, 18, 7, 5)
    assert task.recur_dtstart.tzinfo is None


def test_next_slot_is_strictly_after() -> None:
    task = Task(rrule="FREQ=DAILY", recur_dtstart=datetime(2026, 3, 18, 9, 0))
    at_slot = datetime(2026, 3, 19, 9, 0, tzinfo=UTC)
    assert recurrence.next_slot(task, at_slot, TZ) == datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
