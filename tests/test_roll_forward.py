"""SPEC 7.1: roll_forward. The clock starts on Wed 18 March 2026, 10:00 London."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from task_orchard.core import recurrence
from task_orchard.core.clock import FixedClock
from task_orchard.core.enums import Status
from task_orchard.core.service import Orchard
from task_orchard.db.models import Task
from tests.helpers import events, last_event_id


def weekly(orchard: Orchard, **kw: Any) -> int:
    """Every Friday at 17:00, appearing two days ahead: the window opens Wednesday 17:00."""
    spec = {"mode": "scheduled", "rrule": "FREQ=WEEKLY;BYDAY=FR", "due_time": "17:00"}
    spec = {**spec, "lead_days": 2, **kw}
    return orchard.create_task("Weekly summary", recurrence=spec)["task"]["id"]


def instances(orchard: Orchard, recurring_id: int) -> list[Task]:
    with orchard.tx() as tx:
        rows = list(
            tx.session.scalars(
                sa.select(Task).where(Task.parent_id == recurring_id).order_by(Task.id)
            )
        )
        tx.session.expunge_all()
        return rows


def live(orchard: Orchard, recurring_id: int) -> list[Task]:
    return [t for t in instances(orchard, recurring_id) if t.status is Status.OPEN]


def count_raw(orchard: Orchard, kind: str) -> int:
    """Count events without opening an Orchard transaction, which would roll forward."""
    with orchard.engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT count(*) FROM events WHERE type = :kind"), {"kind": kind}
        ).scalar_one()


def test_the_first_instance_appears_when_the_lead_window_opens(
    orchard: Orchard, clock: FixedClock
) -> None:
    summary = weekly(orchard)
    assert instances(orchard, summary) == []
    clock.advance(hours=6, minutes=59)  # Wed 16:59
    orchard.get_critical()
    assert instances(orchard, summary) == []
    clock.advance(minutes=1)  # Wed 17:00: Friday 17:00 minus two days
    critical = orchard.get_critical(horizon_days=3)["critical"]
    [instance] = instances(orchard, summary)
    assert instance.title == "Weekly summary"
    assert instance.triaged is False
    assert instance.occurrence_at == instance.deadline
    assert instance.deadline_firmness.value == "hard"
    assert instance.estimate_minutes is None and instance.priority is None
    assert instance.motive is None and instance.pattern_id is None
    assert instance.field_sources == {"deadline": "user"}
    row = next(r for r in critical if r["id"] == instance.id)
    assert row["instance_of"] == summary
    assert "inbox" in row["state"]
    assert row["due"]["at"] == "2026-03-20T17:00"


def test_spawning_is_idempotent(orchard: Orchard, clock: FixedClock) -> None:
    summary = weekly(orchard)
    clock.advance(days=1)
    orchard.get_critical()
    since = last_event_id(orchard)
    with orchard.tx() as tx:
        recurrence.roll_forward(tx.session, tx.now, tx.tz)
        recurrence.roll_forward(tx.session, tx.now, tx.tz)
    orchard.get_critical()
    assert events(orchard, since) == []
    assert len(instances(orchard, summary)) == 1


def test_spawned_events(orchard: Orchard, clock: FixedClock) -> None:
    summary = weekly(orchard)
    since = last_event_id(orchard)
    clock.advance(days=1)
    orchard.get_critical()
    [instance] = instances(orchard, summary)
    assert events(orchard, since) == [
        ("spawned", instance.id, {"recurring_id": summary, "occurrence_at": "2026-03-20T17:00"})
    ]


def test_the_cap_closes_the_oldest_as_missed(orchard: Orchard, clock: FixedClock) -> None:
    summary = weekly(orchard)
    clock.advance(days=1)
    orchard.get_critical()
    clock.advance(weeks=1)
    orchard.get_critical()
    first, second = live(orchard, summary)
    since = last_event_id(orchard)
    clock.advance(weeks=1)
    orchard.get_critical()
    third = instances(orchard, summary)[-1]
    assert [t.id for t in live(orchard, summary)] == [second.id, third.id]
    assert instances(orchard, summary)[0].status is Status.MISSED
    assert events(orchard, since) == [
        ("missed", first.id, {"recurring_id": summary, "replaced_by": third.id}),
        ("spawned", third.id, {"recurring_id": summary, "occurrence_at": "2026-04-03T17:00"}),
    ]


def test_a_missed_instance_takes_its_open_descendants(orchard: Orchard, clock: FixedClock) -> None:
    summary = weekly(orchard)
    clock.advance(days=1)
    orchard.get_critical()
    [first] = instances(orchard, summary)
    steps = orchard.subdivide_task(first.id, [{"title": "Draft"}, {"title": "Send"}])["children"]
    orchard.complete_task(steps[0]["id"])
    clock.advance(weeks=2)
    orchard.get_critical()
    assert orchard.get_task(first.id)["task"]["status"] == "missed"
    assert orchard.get_task(steps[0]["id"])["task"]["status"] == "done"
    assert orchard.get_task(steps[1]["id"])["task"]["status"] == "dropped"
    assert events(orchard)[-3][:2] == ("dropped", steps[1]["id"])
    assert events(orchard)[-3][2] == {"reason": "instance missed", "cascade_from": first.id}


def test_lockstep_whatever_happened_to_the_last_instance(
    orchard: Orchard, clock: FixedClock
) -> None:
    early, late, never = weekly(orchard), weekly(orchard), weekly(orchard)
    clock.advance(days=1)  # Thursday: this week's instances exist
    orchard.get_critical()
    orchard.complete_task(live(orchard, early)[0].id)  # done early, on Thursday
    clock.advance(days=3)  # Sunday, after the Friday slot
    orchard.complete_task(live(orchard, late)[0].id)  # done late
    clock.advance(days=3)  # Wednesday 17:00 next week: the window opens for all three
    clock.advance(hours=7)
    orchard.get_critical()
    for recurring in (early, late, never):
        slots = [t.occurrence_at for t in instances(orchard, recurring)]
        assert [s.date().isoformat() for s in slots] == ["2026-03-20", "2026-03-27"]


def test_a_long_absence_leaves_two_open_and_the_rest_missed(
    orchard: Orchard, clock: FixedClock
) -> None:
    summary = weekly(orchard)
    clock.advance(weeks=10)
    orchard.get_critical()
    every = instances(orchard, summary)
    # Ten Fridays have opened their window (20 March to 22 May).
    assert len(every) == 10
    assert [t.status for t in every].count(Status.OPEN) == 2
    assert [t.status for t in every].count(Status.MISSED) == 8


def test_a_pause_skips_slots_without_marking_them_missed(
    orchard: Orchard, clock: FixedClock
) -> None:
    summary = weekly(orchard)
    since = last_event_id(orchard)
    orchard.snooze_task(summary, until="2026-04-08", reason="holiday")  # a Wednesday
    clock.advance(weeks=2)
    orchard.get_critical()
    assert instances(orchard, summary) == []  # paused: nothing spawns
    clock.set(clock.now() + timedelta(days=7, hours=8))  # Wed 8 April, 18:00 BST
    orchard.get_critical()
    [instance] = instances(orchard, summary)
    assert instance.occurrence_at.date().isoformat() == "2026-04-10"
    kinds = [e[0] for e in events(orchard, since)]
    assert kinds == ["paused", "resumed", "spawned"]
    assert "missed" not in kinds
    assert events(orchard, since)[1][2] == {"skipped_until": "2026-04-08T00:00"}


def test_waking_a_pause_resumes_from_now(orchard: Orchard, clock: FixedClock) -> None:
    summary = weekly(orchard)
    orchard.snooze_task(summary, days=30)
    clock.advance(weeks=2)  # Wed 1 April 10:00: two Fridays skipped
    orchard.snooze_task(summary, days=0)
    assert events(orchard)[-1] == ("resumed", summary, {"skipped_until": "2026-04-01T11:00"})
    clock.advance(hours=7)  # 18:00 BST: the window for Friday 3 April is open
    orchard.get_critical()
    assert [t.occurrence_at.date().isoformat() for t in instances(orchard, summary)] == [
        "2026-04-03"
    ]


def test_a_snooze_on_an_ancestor_does_not_stop_spawning(
    orchard: Orchard, clock: FixedClock
) -> None:
    area = orchard.create_task("Work")["task"]["id"]
    summary = orchard.create_task(
        "Weekly summary",
        parent_id=area,
        recurrence={"mode": "scheduled", "rrule": "FREQ=WEEKLY;BYDAY=FR", "lead_days": 2},
    )["task"]["id"]
    orchard.snooze_task(area, days=30)
    clock.advance(days=1)
    orchard.get_critical()
    [instance] = instances(orchard, summary)
    assert "dormant" in orchard.get_task(instance.id)["task"]["state"]


def test_a_rule_that_runs_out_retires_its_task(orchard: Orchard, clock: FixedClock) -> None:
    twice = weekly(orchard, rrule="FREQ=WEEKLY;BYDAY=FR;COUNT=2")
    clock.advance(weeks=1, days=1)
    orchard.get_critical()
    assert len(live(orchard, twice)) == 2
    # The rule has no slots left, but instances are open: it waits.
    assert orchard.get_task(twice)["task"].get("status") is None
    since = last_event_id(orchard)
    for instance in live(orchard, twice):
        orchard.complete_task(instance.id)
    orchard.get_critical()
    assert orchard.get_task(twice)["task"]["status"] == "dropped"
    assert ("dropped", twice, {"reason": "schedule ended"}) in events(orchard, since)


def test_sixty_per_request_then_the_next_request_continues(
    orchard: Orchard, clock: FixedClock
) -> None:
    daily = orchard.create_task(
        "Daily", recurrence={"mode": "scheduled", "rrule": "FREQ=DAILY", "due_time": "12:00"}
    )["task"]["id"]
    clock.advance(days=100)
    orchard.get_critical()
    assert count_raw(orchard, "spawned") == 60
    orchard.get_critical()
    total = count_raw(orchard, "spawned")
    assert total > 60
    assert count_raw(orchard, "missed") == total - 2
    with orchard.tx() as tx:
        recurring = tx.session.get(Task, daily)
        upcoming = recurrence.next_slot(recurring, recurring.last_spawned_slot, tx.tz)
        assert upcoming - timedelta(days=1) > tx.now  # caught up
