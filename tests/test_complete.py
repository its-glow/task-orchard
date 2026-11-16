"""SPEC 8.7: complete_task (recurrence arrives in Phase 4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from task_orchard.core.enums import FieldSource, Level, Motive
from task_orchard.core.errors import (
    HasOpenChildren,
    Invalid,
    NotOpen,
    ParentClosed,
)
from task_orchard.core.service import Orchard
from tests.conftest import NOW
from tests.helpers import completion, counts, make


def test_the_completion_row_holds_the_facts_and_snapshots(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    result = orchard.complete_task(
        seeded["game_build"],
        actual_minutes=75,
        felt_activation="High",
        felt_drain="low",
        reflection="  Easier than I feared.  ",
    )
    row = result["completed"]
    assert row["status"] == "done"
    assert "motive" not in row  # a closed task shows only its own values (SPEC 5.4)
    done = completion(orchard, seeded["game_build"])
    assert done.completed_at == NOW
    assert done.actual_minutes == 75
    assert done.felt_activation is Level.HIGH
    assert done.felt_drain is Level.LOW
    assert done.reflection == "Easier than I feared."
    assert done.estimate_at_completion == 60
    assert done.estimate_source_at_completion is FieldSource.GUESS
    # The EFFECTIVE motive, inherited from "Make my game", as it was then.
    assert done.motive_at_completion is Motive.WANT


def test_the_estimate_source_is_snapshotted(orchard: Orchard, seeded: dict[str, int]) -> None:
    orchard.complete_task(seeded["dentist"])
    assert completion(orchard, seeded["dentist"]).estimate_source_at_completion is (
        FieldSource.PATTERN
    )
    task = make(orchard)
    orchard.complete_task(task)
    done = completion(orchard, task)
    assert done.estimate_at_completion is None
    assert done.estimate_source_at_completion is None
    assert done.motive_at_completion is None


def test_the_task_is_closed_and_shows_its_completion(orchard: Orchard) -> None:
    task = make(orchard)
    orchard.complete_task(task, actual_minutes=0)
    detail = orchard.get_task(task)["task"]
    assert detail["status"] == "done"
    assert detail["closed"]["at"] == "2026-03-18T10:00"
    assert detail["completion"]["completed"]["at"] == "2026-03-18T10:00"


def test_completed_at_may_be_in_the_past(orchard: Orchard) -> None:
    task = make(orchard)
    orchard.complete_task(task, completed_at="2026-03-16")
    assert completion(orchard, task).completed_at == datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
    detail = orchard.get_task(task)["task"]
    assert detail["closed"]["at"] == "2026-03-18T10:00"  # when it was logged

    task = make(orchard)
    orchard.complete_task(task, completed_at="today")  # midday is still to come
    assert completion(orchard, task).completed_at == NOW


def test_completed_at_in_the_future_is_rejected(orchard: Orchard) -> None:
    task = make(orchard)
    before = counts(orchard)
    with pytest.raises(Invalid):
        orchard.complete_task(task, completed_at="+2h")
    assert counts(orchard) == before


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"task_id": 1, "title": "Both"},
        {"task_id": 1, "parent_id": 2},
        {"task_id": 1, "motive": "want"},
        {"task_id": 1, "pattern_id": 1, "new_pattern": "Calls"},
        {"task_id": 1, "actual_minutes": -5},
        {"task_id": 1, "felt_drain": "extreme"},
        {"title": "  "},
    ],
)
def test_invalid_combinations(orchard: Orchard, kw: dict) -> None:
    make(orchard)
    make(orchard)
    before = counts(orchard)
    with pytest.raises(Invalid):
        orchard.complete_task(**kw)
    assert counts(orchard) == before


def test_a_closed_task_cannot_be_completed(orchard: Orchard) -> None:
    task = make(orchard)
    orchard.complete_task(task)
    with pytest.raises(NotOpen) as caught:
        orchard.complete_task(task)
    assert "reopen_task" in caught.value.hint


def test_an_instance_completes_as_a_plain_task(orchard: Orchard, seeded: dict[str, int]) -> None:
    result = orchard.complete_task(seeded["summary_w3"])
    assert result["completed"]["status"] == "done"
    assert "parent_check" not in result  # the parent is recurring, not plain


def test_a_blocked_or_dormant_task_can_be_completed(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    orchard.complete_task(seeded["release_publish"])
    orchard.complete_task(seeded["taxes"])


# --- Untracked work ---


def test_untracked_work_is_created_and_completed(orchard: Orchard) -> None:
    result = orchard.complete_task(
        title="Fixed the garden gate", motive="should", description="Hinge", actual_minutes=20
    )
    row = result["completed"]
    assert row["title"] == "Fixed the garden gate"
    assert row["status"] == "done"
    detail = orchard.get_task(row["id"])["task"]
    assert detail["description"] == "Hinge"
    assert detail["fields"]["motive"] == {"value": "should", "source": "guess"}
    assert "inbox" not in detail.get("state", [])
    assert completion(orchard, row["id"]).motive_at_completion is Motive.SHOULD


def test_untracked_work_under_a_parent_flips_its_achievable(orchard: Orchard) -> None:
    parent = make(orchard, "Garden", achievable="yes", motive="want")
    result = orchard.complete_task(title="Weeded the beds", parent_id=parent)
    assert completion(orchard, result["completed"]["id"]).motive_at_completion is Motive.WANT
    detail = orchard.get_task(parent)["task"]
    assert detail["achievable"] == "no"
    assert detail["fields"]["achievable"]["source"] == "evidence"
    # The parent now has a done child and nothing open: worth a check.
    assert result["parent_check"]["parent"]["id"] == parent
    assert result["parent_check"]["children"] == [{"title": "Weeded the beds", "status": "done"}]


def test_untracked_work_needs_an_open_plain_parent(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    closed = make(orchard)
    orchard.complete_task(closed)
    with pytest.raises(ParentClosed):
        orchard.complete_task(title="X", parent_id=closed)
    with pytest.raises(Invalid) as caught:
        orchard.complete_task(title="X", parent_id=seeded["laundry"])
    assert "task_id" in caught.value.hint


# --- Children ---


def test_open_children_block_completion(orchard: Orchard) -> None:
    parent = make(orchard)
    child = make(orchard, "Child", parent_id=parent)
    before = counts(orchard)
    with pytest.raises(HasOpenChildren) as caught:
        orchard.complete_task(parent)
    assert caught.value.details["children"] == [{"id": child, "title": "Child"}]
    assert "drop_open_children" in caught.value.hint
    assert counts(orchard) == before


def test_drop_open_children_drops_every_open_descendant(orchard: Orchard) -> None:
    parent = make(orchard)
    child = make(orchard, "Child", parent_id=parent)
    grandchild = make(orchard, "Grandchild", parent_id=child)
    done = make(orchard, "Done child", parent_id=parent)
    orchard.complete_task(done)
    orchard.complete_task(parent, drop_open_children=True)
    assert orchard.get_task(child)["task"]["status"] == "dropped"
    assert orchard.get_task(grandchild)["task"]["status"] == "dropped"
    assert orchard.get_task(done)["task"]["status"] == "done"
    assert orchard.get_task(parent)["task"]["status"] == "done"


# --- parent_check ---


def test_parent_check_appears_when_the_last_child_closes(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    first = orchard.complete_task(seeded["game_movement"])
    assert "parent_check" not in first
    second = orchard.complete_task(seeded["game_build"])
    check = second["parent_check"]
    assert check["parent"]["id"] == seeded["game"]
    assert check["parent"]["achievable"] == "no"
    assert [c["title"] for c in check["children"]] == [
        "Fix the build pipeline",
        "Prototype the movement system",
    ]
    assert {c["status"] for c in check["children"]} == {"done"}


def test_no_parent_check_for_a_root(orchard: Orchard) -> None:
    assert "parent_check" not in orchard.complete_task(make(orchard))


def test_a_parent_left_with_nothing_open_is_offered_as_not_achievable(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    orchard.complete_task(seeded["game_movement"])
    orchard.complete_task(seeded["game_build"])
    rows = orchard.get_candidates(under_id=seeded["game"])["candidates"]
    assert [r["id"] for r in rows] == [seeded["game"]]
    assert rows[0]["achievable"] == "no"


# --- unblocked ---


def test_unblocked_lists_tasks_now_free(orchard: Orchard, seeded: dict[str, int]) -> None:
    result = orchard.complete_task(seeded["release_flaky"])
    assert [r["id"] for r in result["unblocked"]] == [seeded["release_notes"]]
    assert "blocked" not in result["unblocked"][0].get("state", [])


def test_unblocked_skips_tasks_still_waiting_on_another_blocker(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    # Publish waits on the notes and on the certificate.
    assert "unblocked" not in orchard.complete_task(seeded["cert"])


def test_unblocked_includes_descendants_freed_through_a_parent(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    result = orchard.complete_task(seeded["budget"])
    # The offsite and its venue are free; the invitation still waits on the venue.
    assert [r["id"] for r in result["unblocked"]] == [seeded["offsite"], seeded["offsite_venue"]]


def test_unblocked_is_capped(orchard: Orchard) -> None:
    blocker = make(orchard, "Blocker")
    blocked = [make(orchard, f"Waits {i}", blocked_by=[blocker]) for i in range(12)]
    result = orchard.complete_task(blocker)
    assert [r["id"] for r in result["unblocked"]] == blocked[:10]
    assert result["unblocked_more"] == 2


def test_completing_with_a_new_pattern_files_the_task(orchard: Orchard) -> None:
    task = make(orchard)
    orchard.complete_task(task, new_pattern="  Garden   jobs ")
    again = make(orchard)
    orchard.complete_task(again, new_pattern="garden jobs")  # reused, ignoring case
    first = orchard.get_task(task)["task"]["pattern"]
    second = orchard.get_task(again)["task"]["pattern"]
    assert first["name"] == "Garden jobs"
    assert first["id"] == second["id"]
    assert second["done"] == 2
