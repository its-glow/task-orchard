"""SPEC 7.1 and 7.2: completing recurring tasks, and flexible instances."""

from __future__ import annotations

from typing import Any

import pytest

from task_orchard.core.clock import FixedClock
from task_orchard.core.errors import InstanceExists
from task_orchard.core.service import Orchard
from tests.helpers import events, last_event_id, make
from tests.test_roll_forward import instances, live, weekly


def flexible(orchard: Orchard, **kw: Any) -> int:
    spec = {"mode": "flexible", "cadence": "about every five days", **kw}
    return orchard.create_task("Laundry", recurrence=spec, motive="should")["task"]["id"]


# --- Scheduled ---


def test_completing_a_scheduled_task_completes_its_oldest_instance(
    orchard: Orchard, clock: FixedClock
) -> None:
    summary = weekly(orchard)
    clock.advance(weeks=1, days=1)
    orchard.get_critical()
    oldest, newer = live(orchard, summary)
    result = orchard.complete_task(summary)
    assert result["completed"]["id"] == oldest.id
    assert [t.id for t in live(orchard, summary)] == [newer.id]
    assert result["next_slot"]["at"] == "2026-04-03T17:00"
    assert orchard.get_task(summary)["task"].get("status") is None  # never completed


def test_completing_early_consumes_the_next_slot(orchard: Orchard, clock: FixedClock) -> None:
    summary = weekly(orchard)
    clock.advance(days=-1)  # Tuesday: Friday's instance does not exist yet
    since = last_event_id(orchard)
    result = orchard.complete_task(summary, actual_minutes=20)
    [instance] = instances(orchard, summary)
    assert result["completed"]["id"] == instance.id
    assert instance.occurrence_at.date().isoformat() == "2026-03-20"
    assert instance.triaged is True
    assert [e[0] for e in events(orchard, since)] == ["spawned", "completed"]
    assert events(orchard, since)[1][2]["recurring_id"] == summary
    assert result["next_slot"]["at"] == "2026-03-27T17:00"
    # Wednesday 17:00 comes and goes: Friday's slot is not spawned again.
    clock.advance(days=1, hours=8)
    orchard.get_critical()
    assert len(instances(orchard, summary)) == 1


def test_a_scheduled_instance_reports_the_next_slot(orchard: Orchard, clock: FixedClock) -> None:
    summary = weekly(orchard)
    clock.advance(days=1)
    orchard.get_critical()
    [instance] = live(orchard, summary)
    result = orchard.complete_task(instance.id)
    assert result["next_slot"]["at"] == "2026-03-27T17:00"
    assert "recurrence_check" not in result and "parent_check" not in result


# --- Flexible ---


def test_completing_a_flexible_task_directly_creates_a_done_instance(
    orchard: Orchard, clock: FixedClock
) -> None:
    laundry = flexible(orchard)
    since = last_event_id(orchard)
    result = orchard.complete_task(laundry, actual_minutes=15, new_pattern="Chores")
    [instance] = instances(orchard, laundry)
    assert result["completed"]["id"] == instance.id
    assert instance.title == "Laundry" and instance.triaged is True
    assert instance.occurrence_at is None and instance.deadline is None
    assert orchard.get_task(instance.id)["task"]["pattern"]["name"] == "Chores"
    detail = orchard.get_task(laundry)["task"]
    assert "status" not in detail  # the recurring task stays open
    assert "pattern" not in detail  # and is filed under nothing
    assert [e[0] for e in events(orchard, since)] == ["created", "pattern_saved", "completed"]


def test_completing_through_an_open_instance(orchard: Orchard) -> None:
    laundry = flexible(orchard)
    instance = make(orchard, parent_id=laundry, deadline="friday")
    assert orchard.complete_task(laundry)["completed"]["id"] == instance
    other = make(orchard, parent_id=laundry)
    assert orchard.complete_task(other)["completed"]["id"] == other


def test_gap_days_snoozes_from_the_completion(orchard: Orchard) -> None:
    laundry = flexible(orchard, gap_days=2)
    result = orchard.complete_task(laundry, completed_at="2026-03-17")
    # Done Tuesday midday; two days later is Thursday midday; back at the start of Thursday.
    assert result["recurrence_check"]["sleeping_until"]["at"] == "2026-03-19T00:00"
    assert events(orchard)[-1][2]["sleeping_until"] == "2026-03-19T00:00"
    assert "dormant" in orchard.get_task(laundry)["task"]["state"]


def test_without_gap_days_the_snooze_is_left_alone(orchard: Orchard) -> None:
    laundry = flexible(orchard)
    result = orchard.complete_task(laundry)
    assert "sleeping_until" not in result["recurrence_check"]
    assert "dormant" not in orchard.get_task(laundry)["task"].get("state", [])


def test_the_recurrence_check(orchard: Orchard, clock: FixedClock) -> None:
    laundry = flexible(orchard, gap_days=3)
    for _ in range(6):
        orchard.complete_task(laundry)
        clock.advance(days=4)
    check = orchard.complete_task(laundry)["recurrence_check"]
    assert check["recurring"]["id"] == laundry
    assert check["cadence"] == "about every five days"
    assert check["gap_days"] == 3
    assert check["sleeping_until"]["at"] == "2026-04-14T00:00"  # Sat 11th + 3 days
    assert check["recent"] == [
        {"when": "now"},
        {"when": "4 days ago"},
        {"when": "8 days ago"},
        {"when": "12 days ago"},
        {"when": "16 days ago"},
    ]
    assert check["gaps_days"] == [4.0, 4.0, 4.0, 4.0, 4.0]
    assert "update_task" in check["note"]


# --- Instances under a flexible task ---


def test_one_open_instance_at_a_time(orchard: Orchard) -> None:
    laundry = flexible(orchard)
    first = make(orchard, parent_id=laundry)
    with pytest.raises(InstanceExists) as caught:
        make(orchard, parent_id=laundry)
    assert caught.value.details["instance_id"] == first


def test_an_instance_copies_only_the_title(orchard: Orchard) -> None:
    laundry = orchard.create_task(
        "Laundry",
        recurrence={"mode": "flexible", "gap_days": 5},
        priority="high",
        estimate_minutes=40,
        drain="low",
        motive="should",
    )["task"]["id"]
    instance = orchard.create_task(parent_id=laundry, deadline="thursday")["task"]
    assert instance["title"] == "Laundry"
    assert instance["instance_of"] == laundry
    assert set(instance["fields"]) == {"deadline"}
    assert "estimate_minutes" not in instance and "drain" not in instance
    assert instance["priority"] == "high"  # inherited, not copied
    assert instance["priority_via"]["id"] == laundry
    assert instance["motive"] == "should"


def test_creating_an_instance_wakes_a_sleeping_flexible_task(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    run = seeded["run"]
    assert "dormant" in orchard.get_task(run)["task"]["state"]
    since = last_event_id(orchard)
    instance = orchard.create_task(parent_id=run, deadline="friday")["task"]
    assert "dormant" not in instance.get("state", [])
    assert "dormant" not in orchard.get_task(run)["task"].get("state", [])
    kinds = [(e[0], e[1]) for e in events(orchard, since)]
    assert kinds == [("created", instance["id"]), ("updated", run)]
    assert events(orchard, since)[1][2]["changes"]["snooze_until"]["to"] is None


def test_a_flexible_task_reappears_as_a_candidate_when_awake(
    orchard: Orchard, clock: FixedClock
) -> None:
    laundry = flexible(orchard, gap_days=2)
    orchard.complete_task(laundry)
    pool = {r["id"] for r in orchard.get_candidates(limit=30)["candidates"]}
    assert laundry not in pool
    clock.advance(days=5)
    rows = orchard.get_candidates(limit=30)["candidates"]
    row = next(r for r in rows if r["id"] == laundry)
    # Asleep until the start of Friday 20th; now is Monday 23rd, 10:00.
    assert row["recurring"]["past_wake_days"] == 3
    assert row["recurring"]["last_done"] == "5 days ago"
    assert row["recurring"]["done_count"] == 1
    assert row["recurring"]["mode"] == "flexible"


def test_a_flexible_task_with_an_open_instance_is_not_a_candidate(orchard: Orchard) -> None:
    laundry = flexible(orchard)
    instance = make(orchard, parent_id=laundry)
    pool = {r["id"] for r in orchard.get_candidates(limit=30)["candidates"]}
    assert laundry not in pool and instance in pool


def test_a_scheduled_row_shows_its_next_slot(orchard: Orchard) -> None:
    summary = weekly(orchard)
    row = orchard.get_task(summary)["task"]
    assert row["recurring"]["next_slot"]["at"] == "2026-03-20T17:00"
    orchard.snooze_task(summary, until="2026-03-25")
    row = orchard.get_task(summary)["task"]
    assert row["recurring"]["next_slot"]["at"] == "2026-03-27T17:00"  # after the pause
