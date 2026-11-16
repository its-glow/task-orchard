"""SPEC 8.6, 8.8, 8.9, 8.10 and the per-task counts of 5.6."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from task_orchard.core.clock import FixedClock
from task_orchard.core.enums import EventType
from task_orchard.core.errors import (
    AlreadyOpen,
    Invalid,
    NotFound,
    NotOpen,
    ParentClosed,
)
from task_orchard.core.events import record
from task_orchard.core.service import Orchard
from tests.helpers import completion, counts, events, last_event_id, make


def row(orchard: Orchard, task_id: int) -> dict[str, Any]:
    """The task's row keys, as the detail carries them."""
    return orchard.get_task(task_id)["task"]


# --- start_task ---


def test_start_records_a_start_and_counts_them(orchard: Orchard) -> None:
    task = make(orchard)
    assert orchard.start_task(task)["starts"] == 1
    result = orchard.start_task(task)
    assert result["starts"] == 2
    assert result["task"]["starts"] == 2


def test_start_rules(orchard: Orchard, seeded: dict[str, int]) -> None:
    orchard.start_task(seeded["release_publish"])  # blocked: the user's actions are facts
    orchard.start_task(seeded["taxes"])  # dormant
    orchard.start_task(seeded["laundry"])  # a flexible task is worked on as itself
    with pytest.raises(Invalid):
        orchard.start_task(seeded["summary"])  # a scheduled one through its occurrence
    done = make(orchard)
    orchard.complete_task(done)
    with pytest.raises(NotOpen):
        orchard.start_task(done)


# --- snooze_task ---


def test_snooze_until_hides_the_task(orchard: Orchard) -> None:
    task = make(orchard)
    result = orchard.snooze_task(task, until="friday", reason="not this week")
    detail = result["task"]
    assert "dormant" in detail["state"]
    assert detail["snoozed_until"]["at"] == "2026-03-20T00:00"  # the start of Friday
    assert detail["deferred"] == 1
    assert events(orchard)[-1] == (
        "snoozed",
        task,
        {"until": "2026-03-20T00:00", "reason": "not this week"},
    )


def test_snooze_days_ends_at_the_start_of_a_day(orchard: Orchard) -> None:
    task = make(orchard)
    detail = orchard.snooze_task(task, days=2)["task"]
    assert detail["snoozed_until"]["at"] == "2026-03-20T00:00"


def test_snoozing_again_records_the_previous_time(orchard: Orchard) -> None:
    task = make(orchard)
    orchard.snooze_task(task, days=2)
    orchard.snooze_task(task, days=5)
    assert events(orchard)[-1][2] == {"until": "2026-03-23T00:00", "previous": "2026-03-20T00:00"}
    assert row(orchard, task)["deferred"] == 2


def test_days_zero_wakes_the_task(orchard: Orchard) -> None:
    task = make(orchard)
    orchard.snooze_task(task, days=3)
    detail = orchard.snooze_task(task, days=0)["task"]
    assert "dormant" not in detail.get("state", [])
    assert events(orchard)[-1] == ("woken", task, {"previous": "2026-03-21T00:00"})
    assert row(orchard, task)["deferred"] == 1  # waking is not a deferral


def test_a_past_time_wakes_the_task(orchard: Orchard) -> None:
    task = make(orchard)
    orchard.snooze_task(task, days=3)
    orchard.snooze_task(task, until="2026-03-01")
    assert events(orchard)[-1][0] == "woken"


def test_waking_an_awake_task_changes_nothing(orchard: Orchard) -> None:
    task = make(orchard)
    before = counts(orchard)
    orchard.snooze_task(task, days=0)
    assert counts(orchard) == before


@pytest.mark.parametrize(
    "kw", [{}, {"until": "friday", "days": 2}, {"days": 1.5}, {"days": -1}, {"days": True}]
)
def test_snooze_needs_exactly_one_whole_time(orchard: Orchard, kw: dict) -> None:
    task = make(orchard)
    with pytest.raises(Invalid):
        orchard.snooze_task(task, **kw)


def test_snooze_rules(orchard: Orchard, seeded: dict[str, int]) -> None:
    done = make(orchard)
    orchard.complete_task(done)
    with pytest.raises(NotOpen):
        orchard.snooze_task(done, days=2)


def test_update_snooze_until_is_not_a_deferral(orchard: Orchard) -> None:
    task = make(orchard)
    orchard.update_task(task, snooze_until="friday")
    assert "deferred" not in row(orchard, task)
    assert "dormant" in row(orchard, task)["state"]


# --- drop_tasks ---


def test_drop_cascades_to_open_descendants(orchard: Orchard) -> None:
    parent = make(orchard, "Parent")
    child = make(orchard, "Child", parent_id=parent)
    grandchild = make(orchard, "Grandchild", parent_id=child)
    done = make(orchard, "Done", parent_id=parent)
    orchard.complete_task(done)
    since = last_event_id(orchard)
    result = orchard.drop_tasks([parent], reason="  not needed ")
    assert result["dropped"] == [{"id": parent, "title": "Parent"}]
    assert result["also_dropped"] == [
        {"id": child, "title": "Child"},
        {"id": grandchild, "title": "Grandchild"},
    ]
    assert events(orchard, since) == [
        ("dropped", parent, {"reason": "not needed"}),
        ("dropped", child, {"reason": "not needed", "cascade_from": parent}),
        ("dropped", grandchild, {"reason": "not needed", "cascade_from": parent}),
    ]
    assert orchard.get_task(done)["task"]["status"] == "done"


def test_closed_tasks_are_skipped(orchard: Orchard) -> None:
    open_task = make(orchard, "Open")
    done = make(orchard, "Done")
    orchard.complete_task(done)
    result = orchard.drop_tasks([open_task, done, open_task], reason="tidy")
    assert result["dropped"] == [{"id": open_task, "title": "Open"}]
    assert result["skipped"] == [{"id": done, "title": "Done", "status": "done"}]


def test_dropping_only_closed_tasks_returns_an_empty_dropped_list(orchard: Orchard) -> None:
    done = make(orchard)
    orchard.complete_task(done)
    before = counts(orchard)
    result = orchard.drop_tasks([done], reason="tidy")
    assert result["dropped"] == []
    assert counts(orchard) == before


def test_a_listed_descendant_is_dropped_in_its_own_right(orchard: Orchard) -> None:
    parent = make(orchard, "Parent")
    child = make(orchard, "Child", parent_id=parent)
    result = orchard.drop_tasks([parent, child], reason="tidy")
    assert [d["id"] for d in result["dropped"]] == [parent, child]
    assert "also_dropped" not in result


def test_drop_reports_unblocked_and_parent_checks(orchard: Orchard, seeded: dict[str, int]) -> None:
    result = orchard.drop_tasks([seeded["release_flaky"]], reason="test removed")
    assert [r["id"] for r in result["unblocked"]] == [seeded["release_notes"]]
    assert "parent_checks" not in result  # the release still has open steps

    result = orchard.drop_tasks([seeded["game_movement"], seeded["game_build"]], reason="shelved")
    [check] = result["parent_checks"]
    assert check["parent"]["id"] == seeded["game"]
    assert {c["status"] for c in check["children"]} == {"dropped"}


def test_drop_validation(orchard: Orchard, seeded: dict[str, int]) -> None:
    task = make(orchard)
    before = counts(orchard)
    for kw in ({"task_ids": [], "reason": "x"}, {"task_ids": [task], "reason": "  "}):
        with pytest.raises(Invalid):
            orchard.drop_tasks(**kw)
    with pytest.raises(NotFound):
        orchard.drop_tasks([task, 9999], reason="x")
    assert counts(orchard) == before


# --- reopen_task ---


def test_reopen_undoes_a_completion_and_keeps_its_data_in_the_event(orchard: Orchard) -> None:
    task = make(orchard, estimate_minutes=30, motive="want")
    orchard.complete_task(
        task,
        actual_minutes=0,
        felt_drain="high",
        reflection="Quick.",
        completed_at="2026-03-17",
    )
    detail = orchard.reopen_task(task)["task"]
    assert "status" not in detail
    assert "closed" not in detail and "completion" not in detail
    assert completion(orchard, task) is None
    assert events(orchard)[-1] == (
        "reopened",
        task,
        {
            "was": "done",
            "completion": {
                "completed_at": "2026-03-17T12:00",
                "actual_minutes": 0,
                "felt_drain": "high",
                "reflection": "Quick.",
                "estimate_at_completion": 30,
                "estimate_source_at_completion": "guess",
                "motive_at_completion": "want",
            },
        },
    )
    orchard.complete_task(task)  # and it can be completed again


def test_reopen_a_dropped_task(orchard: Orchard) -> None:
    parent = make(orchard)
    child = make(orchard, parent_id=parent)
    orchard.drop_tasks([parent], reason="x")
    orchard.reopen_task(parent)
    assert events(orchard)[-1] == ("reopened", parent, {"was": "dropped"})
    # Its children stay as they were.
    assert orchard.get_task(child)["task"]["status"] == "dropped"


def test_reopen_rules(orchard: Orchard, seeded: dict[str, int]) -> None:
    task = make(orchard)
    with pytest.raises(AlreadyOpen):
        orchard.reopen_task(task)
    parent = make(orchard)
    child = make(orchard, parent_id=parent)
    orchard.drop_tasks([parent], reason="x")
    with pytest.raises(ParentClosed) as caught:
        orchard.reopen_task(child)
    assert "reopen" in caught.value.hint


# --- Per-task counts (5.6) ---


def test_seeded_counts_appear_in_rows(orchard: Orchard, seeded: dict[str, int]) -> None:
    dentist = row(orchard, seeded["dentist"])
    assert dentist["deferred"] == 4
    assert dentist["starts"] == 2
    assert dentist["touched"] == "6 days ago"  # the last snooze
    [listed] = orchard.get_candidates(under_id=seeded["dentist"])["candidates"]
    assert listed["deferred"] == 4
    assert listed["pattern"] == {
        "id": dentist["pattern"]["id"],
        "name": "Phone calls to book things",
        "hint": "Hard to start, five minutes once dialled. Goes better straight after lunch.",
    }
    assert "deferred" not in row(orchard, seeded["mechanic"])
    assert "touched" not in row(orchard, seeded["summary"])  # recurring tasks have none


@pytest.fixture
def stale(orchard: Orchard, clock: FixedClock) -> int:
    """A task created ten days ago and not touched since."""
    task = make(orchard)
    clock.advance(days=10)
    assert row(orchard, task)["touched"] == "10 days ago"
    return task


TOUCHES: dict[str, Callable[[Orchard, int], Any]] = {
    "started": lambda o, t: o.start_task(t),
    "snoozed": lambda o, t: o.snooze_task(t, until="+3d"),
    "subdivided": lambda o, t: o.subdivide_task(t, [{"title": "Step"}]),
    "user update": lambda o, t: o.update_task(t, priority="high", sources={"priority": "user"}),
}


@pytest.mark.parametrize("touch", TOUCHES)
def test_touch_events_move_last_touched(orchard: Orchard, stale: int, touch: str) -> None:
    TOUCHES[touch](orchard, stale)
    assert "touched" not in row(orchard, stale)


def test_woken_and_reopened_are_touches(orchard: Orchard, clock: FixedClock) -> None:
    task = make(orchard)
    orchard.snooze_task(task, days=20)
    clock.advance(days=10)
    orchard.snooze_task(task, days=0)
    assert "touched" not in row(orchard, task)
    orchard.complete_task(task)
    clock.advance(days=10)
    orchard.reopen_task(task)
    assert "touched" not in row(orchard, task)


@pytest.mark.parametrize(
    "change",
    [
        {"priority": "high"},  # a guess
        {"deadline": "2026-04-30", "sources": {"deadline": "evidence"}},  # housekeeping re-date
        {"title": "Renamed"},  # untracked
    ],
)
def test_agent_edits_are_not_touches(orchard: Orchard, stale: int, change: dict) -> None:
    orchard.update_task(stale, **change)
    assert row(orchard, stale)["touched"] == "10 days ago"


def test_clock_events_are_not_touches(orchard: Orchard, stale: int) -> None:
    with orchard.tx() as tx:
        record(tx.session, EventType.SPAWNED, at=tx.now, task_id=stale, payload={})
        record(tx.session, EventType.MISSED, at=tx.now, task_id=stale, payload={})
    assert row(orchard, stale)["touched"] == "10 days ago"


def test_reopened_reads_as_one_line_in_recent_events(orchard: Orchard) -> None:
    task = make(orchard)
    orchard.complete_task(task, reflection="A long reflection " * 20)
    detail = orchard.reopen_task(task)["task"]
    assert detail["recent_events"][0] == {"type": "reopened", "when": "now", "note": "was done"}
