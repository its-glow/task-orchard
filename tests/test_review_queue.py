"""SPEC 6.5 and 6.6: the review queue and recent activity."""

from __future__ import annotations

from typing import Any

import pytest

from task_orchard.core.clock import FixedClock
from task_orchard.core.errors import Invalid
from task_orchard.core.service import Orchard
from tests.helpers import make
from tests.test_roll_forward import live, weekly

SECTIONS = {
    "inbox", "parent_checks", "overdue", "most_deferred", "repeat_starts",
    "untouched", "duplicates", "conflicts", "missed",
}  # fmt: skip


def queue(orchard: Orchard) -> dict[str, Any]:
    return orchard.get_review_queue()


def ids(section: dict[str, Any]) -> list[int]:
    return [item["id"] for item in section.get("items", [])]


# --- Shape ---


def test_every_section_is_items_and_total(orchard: Orchard) -> None:
    result = queue(orchard)
    assert set(result) == SECTIONS | {"now"}
    for name in SECTIONS:
        assert result[name] == {"items": [], "total": 0}, name


def test_items_are_slim_rows(orchard: Orchard, seeded: dict[str, int]) -> None:
    result = queue(orchard)
    slim = {"id", "title", "path", "instance_of", "overdue_days", "firmness", "deferred",
            "starts", "untouched_days"}  # fmt: skip
    for name in ("inbox", "parent_checks", "overdue", "most_deferred", "repeat_starts",
                 "untouched"):  # fmt: skip
        for item in result[name]["items"]:
            assert set(item) <= slim, (name, item)
            assert "priority" not in item and "state" not in item


# --- Sections ---


def test_inbox_oldest_first_up_to_30(orchard: Orchard, clock: FixedClock) -> None:
    first = orchard.capture_tasks([{"title": f"Old {i}"} for i in range(20)])["captured"]
    clock.advance(minutes=1)
    orchard.capture_tasks([{"title": f"New {i}"} for i in range(20)])
    inbox = queue(orchard)["inbox"]
    assert inbox["total"] == 40
    assert len(inbox["items"]) == 30
    assert ids(inbox)[:20] == [c["id"] for c in first]


def test_inbox_marks_spawned_instances(orchard: Orchard, clock: FixedClock) -> None:
    summary = weekly(orchard)
    clock.advance(days=1)
    [item] = queue(orchard)["inbox"]["items"]
    assert item["instance_of"] == summary


def test_parent_checks(orchard: Orchard) -> None:
    finished = make(orchard, "Finished")
    child = make(orchard, parent_id=finished)
    busy = make(orchard, "Busy")
    make(orchard, parent_id=busy)
    orchard.complete_task(child)
    assert ids(queue(orchard)["parent_checks"]) == [finished]


def test_overdue_leaves_longest_first(orchard: Orchard, clock: FixedClock) -> None:
    a = make(orchard, "A", deadline="2026-03-15", firmness="soft")
    b = make(orchard, "B", deadline="2026-03-10", firmness="hard")
    parent = make(orchard, "Parent", deadline="2026-03-01")  # not a leaf
    make(orchard, parent_id=parent)
    make(orchard, "Future", deadline="2026-03-20")
    items = queue(orchard)["overdue"]["items"]
    assert ids({"items": items}) == [b, a]
    assert items[0]["overdue_days"] == 7 and items[0]["firmness"] == "hard"
    assert items[1]["overdue_days"] == 2 and items[1]["firmness"] == "soft"


def test_deferred_and_restarted(orchard: Orchard) -> None:
    once, thrice, started = make(orchard), make(orchard), make(orchard)
    orchard.snooze_task(once, days=0)  # nothing to wake: not a deferral
    orchard.snooze_task(once, days=1)
    for _ in range(3):
        orchard.snooze_task(thrice, days=1)
    orchard.start_task(started)
    orchard.start_task(started)
    orchard.start_task(once)
    result = queue(orchard)
    assert result["most_deferred"]["items"] == [
        {"id": thrice, "title": "Task", "deferred": 3},
        {"id": once, "title": "Task", "deferred": 1},
    ]
    assert ids(result["repeat_starts"]) == [started]
    assert result["repeat_starts"]["items"][0]["starts"] == 2


def test_untouched_triaged_leaves_oldest_first(orchard: Orchard, clock: FixedClock) -> None:
    old = make(orchard, "Old")
    clock.advance(days=10)
    recent = make(orchard, "Recent")
    asleep = make(orchard, "Asleep", snooze_until="+30d")
    orchard.capture_tasks([{"title": "Untriaged"}])
    orchard.create_task("Run", recurrence={"mode": "flexible"})
    clock.advance(days=2)
    items = queue(orchard)["untouched"]["items"]
    assert ids({"items": items}) == [old, recent]
    assert [i["untouched_days"] for i in items] == [12, 2]
    assert asleep not in ids({"items": items})


def test_duplicates_pairs_but_not_within_a_recurring_family(
    orchard: Orchard, clock: FixedClock
) -> None:
    a = make(orchard, "Call Mum!")
    b = make(orchard, "call  mum")
    make(orchard, "call-mum")
    summary = weekly(orchard, rrule="FREQ=DAILY")
    clock.advance(days=3)  # two open instances, both titled like their parent
    orchard.get_critical()
    assert len(live(orchard, summary)) == 2
    dupes = queue(orchard)["duplicates"]
    assert dupes["items"] == [
        {"a": {"id": a, "title": "Call Mum!"}, "b": {"id": b, "title": "call mum"}}
    ]
    assert dupes["total"] == 1


def test_three_equal_titles_make_three_pairs(orchard: Orchard) -> None:
    a, b, c = (make(orchard, "Buy milk") for _ in range(3))
    pairs = [(p["a"]["id"], p["b"]["id"]) for p in queue(orchard)["duplicates"]["items"]]
    assert pairs == [(a, b), (a, c), (b, c)]


def test_conflicts_from_the_last_30_days_on_open_tasks(orchard: Orchard, clock: FixedClock) -> None:
    task = make(orchard, priority="low", sources={"priority": "user"})
    closed = make(orchard, priority="low", sources={"priority": "user"})
    orchard.update_task(task, priority="high", sources={"priority": "pattern"})
    orchard.update_task(closed, priority="high", sources={"priority": "evidence"})
    orchard.complete_task(closed)
    clock.advance(days=2)
    [item] = queue(orchard)["conflicts"]["items"]
    assert item == {
        "task": {"id": task, "title": "Task"},
        "field": "priority",
        "kept": "low",
        "proposed": "high",
        "source": "pattern",
        "when": "2 days ago",
    }
    clock.advance(days=30)
    assert queue(orchard)["conflicts"]["total"] == 0


def test_missed_per_scheduled_task(orchard: Orchard, clock: FixedClock) -> None:
    summary = weekly(orchard)
    clock.advance(days=1)
    orchard.get_critical()
    orchard.complete_task(summary)
    clock.advance(weeks=3)  # three more Fridays: one of them is closed as missed
    [item] = queue(orchard)["missed"]["items"]
    assert item == {"recurring": {"id": summary, "title": "Weekly summary"}, "missed": 1, "done": 1}


def test_limits_and_totals(orchard: Orchard) -> None:
    for i in range(12):
        task = make(orchard, f"T{i}")
        orchard.snooze_task(task, days=1)
    deferred = queue(orchard)["most_deferred"]
    assert len(deferred["items"]) == 10 and deferred["total"] == 12


# --- Against the seed ---


def test_the_seed(orchard: Orchard, seeded: dict[str, int]) -> None:
    result = queue(orchard)
    assert ids(result["most_deferred"])[0] == seeded["dentist"]
    assert seeded["dentist"] in ids(result["repeat_starts"])
    assert seeded["milk"] in ids(result["overdue"])
    assert seeded["summary"] in [m["recurring"]["id"] for m in result["missed"]["items"]]
    assert result["duplicates"]["total"] == 0


# --- get_recent_activity ---


def test_recent_activity(orchard: Orchard, clock: FixedClock) -> None:
    done = orchard.create_task("Write report", motive="must")["task"]["id"]
    dropped = make(orchard, "Old idea")
    orchard.start_task(done)
    orchard.snooze_task(dropped, days=1)
    orchard.complete_task(done, actual_minutes=50)
    orchard.drop_tasks([dropped], reason="no longer wanted")
    clock.advance(hours=2)
    result = orchard.get_recent_activity(days=1)
    assert result["completed"] == [
        {"id": done, "title": "Write report", "actual_minutes": 50, "motive": "must",
         "when": "2 hours ago"},
    ]  # fmt: skip
    assert result["completed_count"] == 1
    assert result["dropped"] == [
        {"id": dropped, "title": "Old idea", "when": "2 hours ago", "reason": "no longer wanted"}
    ]
    assert result["dropped_count"] == 1
    assert (result["created_count"], result["deferred_count"], result["started_count"]) == (
        2,
        1,
        1,
    )
    assert set(result["balance"]) == {"last_7_days", "last_30_days"}
    clock.advance(days=2)
    later = orchard.get_recent_activity(days=1)
    assert "completed" not in later and "created_count" not in later
    assert later["balance"]["last_7_days"]["minutes"]["must"] == 50


def test_recent_activity_lists_missed_and_reopened_work_drops_out(
    orchard: Orchard, clock: FixedClock
) -> None:
    summary = weekly(orchard)
    clock.advance(weeks=2, days=1)
    orchard.get_critical()
    [missed] = orchard.get_recent_activity(days=7)["missed"]
    assert missed["title"] == "Weekly summary"
    task = make(orchard)
    orchard.complete_task(task)
    orchard.reopen_task(task)
    assert "completed" not in orchard.get_recent_activity(days=1)
    assert summary


@pytest.mark.parametrize("days", [0, 32, -1])
def test_days_is_1_to_31(orchard: Orchard, days: int) -> None:
    with pytest.raises(Invalid):
        orchard.get_recent_activity(days=days)


def test_recent_activity_caps(orchard: Orchard) -> None:
    for i in range(35):
        orchard.complete_task(title=f"Done {i}")
    result = orchard.get_recent_activity()
    assert len(result["completed"]) == 30 and result["completed_count"] == 35
    assert result["completed"][0]["title"] == "Done 34"  # newest first
