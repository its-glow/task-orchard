"""SPEC 7.3, 8.2.1 and 8.3: the shared rules, the recurrence object, conversion and changes."""

from __future__ import annotations

from datetime import datetime

import pytest

from task_orchard.core.clock import FixedClock
from task_orchard.core.errors import (
    InstanceCap,
    InstanceExists,
    Invalid,
    ParentClosed,
    Unsupported,
)
from task_orchard.core.service import Orchard
from task_orchard.db.models import Task
from tests.helpers import counts, events, last_event_id, make
from tests.test_recurring_complete import flexible
from tests.test_roll_forward import instances, live, weekly


def stored(orchard: Orchard, task_id: int) -> Task:
    with orchard.tx() as tx:
        task = tx.session.get(Task, task_id)
        tx.session.expunge(task)
        return task


# --- The recurrence object ---


def test_defaults_for_a_scheduled_task(orchard: Orchard) -> None:
    result = orchard.create_task(
        "Pay rent", recurrence={"mode": "Scheduled", "rrule": "FREQ=MONTHLY;BYMONTHDAY=1"}
    )
    task = stored(orchard, result["task"]["id"])
    assert task.recur_dtstart.time().isoformat() == "23:59:00"
    assert task.lead_minutes == 24 * 60
    assert task.recur_firmness.value == "hard"
    assert task.last_spawned_slot == datetime.fromisoformat("2026-03-18T10:00+00:00")
    assert len(result["next_slots"]) == 3
    assert result["task"]["recurring"]["mode"] == "scheduled"


def test_fractional_lead_days(orchard: Orchard) -> None:
    task_id = weekly(orchard, lead_days=0.5)
    assert stored(orchard, task_id).lead_minutes == 720


@pytest.mark.parametrize(
    "spec",
    [
        {"rrule": "FREQ=DAILY"},  # no mode
        {"mode": "monthly"},
        {"mode": "scheduled"},  # no rrule
        {"mode": "scheduled", "rrule": "FREQ=DAILY", "gap_days": 2},  # wrong mode's key
        {"mode": "flexible", "rrule": "FREQ=DAILY"},
        {"mode": "flexible", "due_time": "09:00"},
        {"mode": "flexible", "gap_days": 0},
        {"mode": "flexible", "gap_days": 1.5},
        {"mode": "scheduled", "rrule": "FREQ=DAILY", "due_time": "25:00"},
        {"mode": "scheduled", "rrule": "FREQ=DAILY", "lead_days": -1},
        {"mode": "scheduled", "rrule": "FREQ=DAILY", "firmness": "rigid"},
    ],
)
def test_invalid_recurrence_objects(orchard: Orchard, spec: dict) -> None:
    before = counts(orchard)
    with pytest.raises(Invalid):
        orchard.create_task("Repeat", recurrence=spec)
    assert counts(orchard) == before


def test_recurrence_with_a_deadline_or_achievable_is_invalid(orchard: Orchard) -> None:
    for extra in ({"deadline": "friday"}, {"achievable": "yes"}):
        with pytest.raises(Invalid):
            orchard.create_task("Repeat", recurrence={"mode": "flexible"}, **extra)


# --- Shared rules (7.3) ---


def test_a_recurring_task_cannot_block(orchard: Orchard, seeded: dict[str, int]) -> None:
    laundry, cert = seeded["laundry"], seeded["cert"]
    with pytest.raises(Unsupported):
        orchard.link_tasks(laundry, cert)
    with pytest.raises(Unsupported):
        make(orchard, blocked_by=[laundry])
    with pytest.raises(Unsupported):
        orchard.create_task("R", recurrence={"mode": "flexible"}, blocks=[cert])


def test_a_recurring_task_can_be_blocked_and_its_instances_inherit_it(
    orchard: Orchard, clock: FixedClock
) -> None:
    blocker = make(orchard, "Get a new printer")
    summary = weekly(orchard)
    orchard.link_tasks(blocker, summary)
    clock.advance(days=1)
    orchard.get_critical()
    [instance] = instances(orchard, summary)
    assert orchard.get_task(instance.id)["task"]["state"][:2] == ["inbox", "blocked"]


def test_no_recurring_task_under_a_recurring_task(orchard: Orchard, seeded: dict[str, int]) -> None:
    laundry, summary = seeded["laundry"], seeded["summary"]
    with pytest.raises(Invalid):
        orchard.create_task("R", parent_id=laundry, recurrence={"mode": "flexible"})
    other = flexible(orchard)
    with pytest.raises(Unsupported):
        orchard.update_task(other, parent_id=summary)
    with pytest.raises(Invalid):  # an instance cannot become recurring
        orchard.update_task(seeded["summary_w3"], recurrence={"mode": "flexible"})


def test_a_recurring_task_under_a_plain_parent_counts_as_an_open_child(
    orchard: Orchard,
) -> None:
    area = make(orchard, "Health")
    orchard.create_task("Run", parent_id=area, recurrence={"mode": "flexible", "gap_days": 2})
    detail = orchard.get_task(area)["task"]
    assert "has_open_children" in detail["state"]
    assert detail["achievable"] == "no"
    critical = {r["id"] for r in orchard.get_critical(horizon_days=30)["critical"]}
    assert area not in critical


def test_dropping_a_recurring_task_retires_it_and_its_instances(
    orchard: Orchard, clock: FixedClock
) -> None:
    summary = weekly(orchard)
    clock.advance(weeks=1, days=1)
    orchard.get_critical()
    result = orchard.drop_tasks([summary], reason="new job")
    assert len(result["also_dropped"]) == 2
    clock.advance(weeks=2)
    orchard.get_critical()
    assert len(instances(orchard, summary)) == 2  # nothing more spawns


def test_a_recurring_task_has_no_achievable(orchard: Orchard, seeded: dict[str, int]) -> None:
    laundry = seeded["laundry"]
    detail = orchard.get_task(laundry)["task"]
    assert "achievable" not in detail and "achievable" not in detail.get("fields", {})
    with pytest.raises(Invalid):
        orchard.update_task(laundry, achievable="yes")
    with pytest.raises(Invalid):
        orchard.update_task(laundry, deadline="friday")
    since = last_event_id(orchard)
    make(orchard, parent_id=laundry)  # adding an instance flips nothing
    assert [e[0] for e in events(orchard, since)] == ["created"]


def test_instances_get_no_pattern_unless_given(orchard: Orchard, clock: FixedClock) -> None:
    pattern = orchard.save_pattern(name="Reports")["pattern"]["id"]
    summary = weekly(orchard)
    orchard.update_task(summary, pattern_id=pattern)
    clock.advance(days=1)
    orchard.get_critical()
    [instance] = instances(orchard, summary)
    assert instance.pattern_id is None
    orchard.complete_task(summary, pattern_id=pattern)
    assert stored(orchard, instance.id).pattern_id == pattern


# --- Conversion (7.3) ---


def test_a_plain_task_can_become_recurring(orchard: Orchard) -> None:
    task = make(orchard, "Water the plants", achievable="yes", estimate_minutes=10)
    since = last_event_id(orchard)
    detail = orchard.update_task(
        task, recurrence={"mode": "flexible", "cadence": "twice a week", "gap_days": 3}
    )["task"]
    assert detail["recurring"] == {"mode": "flexible", "cadence": "twice a week", "gap_days": 3}
    assert "achievable" not in detail
    assert detail["estimate_minutes"] == 10
    [(kind, _, payload)] = events(orchard, since)
    assert kind == "updated"
    assert payload["changes"]["recurrence"] == {
        "from": None,
        "to": {"mode": "flexible", "cadence": "twice a week", "gap_days": 3},
    }


def test_a_deadline_must_be_cleared_in_the_same_call(orchard: Orchard) -> None:
    task = make(orchard, deadline="friday")
    spec = {"mode": "scheduled", "rrule": "FREQ=WEEKLY;BYDAY=FR"}
    with pytest.raises(Invalid):
        orchard.update_task(task, recurrence=spec)
    detail = orchard.update_task(task, recurrence=spec, clear=["deadline"])["task"]
    assert "due" not in detail
    user = make(orchard, deadline="friday", sources={"deadline": "user"})
    with pytest.raises(Invalid) as caught:
        orchard.update_task(user, recurrence=spec, clear=["deadline"])
    assert "user" in caught.value.hint
    orchard.update_task(user, recurrence=spec, clear=["deadline"], sources={"deadline": "user"})


def test_conversion_refusals(orchard: Orchard) -> None:
    spec = {"mode": "flexible"}
    parent = make(orchard)
    make(orchard, parent_id=parent)
    blocker = make(orchard)
    make(orchard, blocked_by=[blocker])
    done = make(orchard)
    orchard.complete_task(done)
    for task in (parent, blocker):
        with pytest.raises(Invalid):
            orchard.update_task(task, recurrence=spec)
    with pytest.raises(Exception, match="done"):
        orchard.update_task(done, recurrence=spec)


# --- Changing a recurring task (8.3) ---


def test_changing_the_rule_restarts_from_today_and_leaves_instances(
    orchard: Orchard, clock: FixedClock
) -> None:
    summary = weekly(orchard)
    clock.advance(days=1)
    orchard.get_critical()
    [instance] = instances(orchard, summary)
    clock.advance(days=1)  # Friday 10:00
    result = orchard.update_task(summary, recurrence={"rrule": "FREQ=WEEKLY;BYDAY=MO"})
    task = stored(orchard, summary)
    assert task.recur_dtstart == datetime(2026, 3, 20, 17, 0)  # keeps the due time
    # max(last_spawned_slot, now): Friday's 17:00 instance is later than now.
    assert task.last_spawned_slot == instance.occurrence_at
    assert result["task"]["recurring"]["next_slot"]["at"] == "2026-03-23T17:00"
    assert [t.id for t in live(orchard, summary)] == [instance.id]
    change = events(orchard)[-1][2]["changes"]["recurrence"]
    assert change["from"]["rrule"] == "FREQ=WEEKLY;BYDAY=FR"
    assert change["to"]["rrule"] == "FREQ=WEEKLY;BYDAY=MO"


def test_partial_changes_and_their_limits(orchard: Orchard) -> None:
    summary = weekly(orchard)
    orchard.update_task(summary, recurrence={"lead_days": 3, "firmness": "soft", "cadence": "x"})
    task = stored(orchard, summary)
    assert task.lead_minutes == 3 * 24 * 60 and task.recur_firmness.value == "soft"
    with pytest.raises(Invalid):
        orchard.update_task(summary, recurrence={"mode": "flexible"})
    with pytest.raises(Invalid):
        orchard.update_task(summary, recurrence={"gap_days": 3})
    with pytest.raises(Invalid):
        orchard.update_task(summary, clear=["gap_days"])
    orchard.update_task(summary, clear=["cadence"])
    assert stored(orchard, summary).cadence is None
    laundry = flexible(orchard, gap_days=4)
    orchard.update_task(laundry, clear=["gap_days"])
    assert stored(orchard, laundry).gap_days is None


def test_update_snooze_until_pauses_and_clearing_resumes(
    orchard: Orchard, clock: FixedClock
) -> None:
    summary = weekly(orchard)
    since = last_event_id(orchard)
    orchard.update_task(summary, snooze_until="2026-04-30")
    clock.advance(days=3)
    orchard.update_task(summary, clear=["snooze_until"])
    kinds = [e[0] for e in events(orchard, since)]
    assert kinds == ["updated", "paused", "updated", "resumed"]
    assert stored(orchard, summary).last_spawned_slot == clock.now()
    # update_task's snooze is scheduling, not a deferral.
    assert "deferred" not in orchard.get_task(summary)["task"]


# --- Reopening (8.10) ---


def test_reopen_respects_the_instance_cap(orchard: Orchard, clock: FixedClock) -> None:
    summary = weekly(orchard)
    clock.advance(days=1)
    orchard.get_critical()
    [first] = live(orchard, summary)
    orchard.complete_task(first.id)
    clock.advance(weeks=2)
    orchard.get_critical()
    assert len(live(orchard, summary)) == 2
    with pytest.raises(InstanceCap):
        orchard.reopen_task(first.id)
    orchard.complete_task(summary)
    orchard.reopen_task(first.id)


def test_reopen_a_flexible_instance(orchard: Orchard) -> None:
    laundry = flexible(orchard, gap_days=5)
    done = orchard.complete_task(laundry)["completed"]["id"]
    result = orchard.reopen_task(done)
    assert "snooze_task" in result["note"]  # the gap snooze is not undone
    orchard.complete_task(done)
    make(orchard, parent_id=laundry)
    with pytest.raises(InstanceExists):
        orchard.reopen_task(done)


def test_reopen_a_retired_scheduled_task_skips_the_missed_slots(
    orchard: Orchard, clock: FixedClock
) -> None:
    summary = weekly(orchard)
    orchard.drop_tasks([summary], reason="break")
    clock.advance(weeks=3)
    orchard.reopen_task(summary)
    assert stored(orchard, summary).last_spawned_slot == clock.now()
    orchard.get_critical()
    assert instances(orchard, summary) == []  # Wednesday 10:00: next window opens at 17:00


def test_an_instance_of_a_closed_recurring_task_cannot_be_reopened(
    orchard: Orchard, clock: FixedClock
) -> None:
    summary = weekly(orchard)
    clock.advance(days=1)
    orchard.get_critical()
    [instance] = live(orchard, summary)
    orchard.complete_task(instance.id)
    orchard.drop_tasks([summary], reason="x")
    with pytest.raises(ParentClosed):
        orchard.reopen_task(instance.id)


# --- Starting and snoozing ---


def test_snoozing_a_flexible_task_is_a_deferral(orchard: Orchard) -> None:
    laundry = flexible(orchard)
    orchard.snooze_task(laundry, days=2)
    assert events(orchard)[-1][0] == "snoozed"
    assert orchard.get_task(laundry)["task"]["deferred"] == 1


def test_snoozing_a_scheduled_task_pauses_it(orchard: Orchard) -> None:
    summary = weekly(orchard)
    orchard.snooze_task(summary, days=10, reason="holiday")
    assert events(orchard)[-1] == (
        "paused",
        summary,
        {"until": "2026-03-28T00:00", "reason": "holiday"},
    )
    orchard.snooze_task(summary, days=0)
    assert events(orchard)[-1][0] == "resumed"
    before = counts(orchard)
    orchard.snooze_task(summary, days=0)  # not paused: nothing to do
    assert counts(orchard) == before
