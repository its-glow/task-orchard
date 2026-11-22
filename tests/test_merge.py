"""SPEC 8.11: merge_tasks."""

from __future__ import annotations

import pytest

from task_orchard.core.errors import CycleError, Invalid, NotOpen, Unsupported
from task_orchard.core.service import Orchard
from task_orchard.core.wire import NOTES_MAX_CHARS
from task_orchard.db.models import Task
from tests.helpers import counts, events, last_event_id, make


def test_children_links_fields_and_notes(orchard: Orchard) -> None:
    pattern = orchard.save_pattern(name="Errands")["pattern"]["id"]
    blocker, waiting = make(orchard, "Blocker"), make(orchard, "Waiting")
    into = make(orchard, "Buy stamps", priority="low", sources={"priority": "user"})
    source = make(
        orchard,
        "buy stamps",
        description="Second class.",
        notes="Post office closes at 5.",
        priority="high",
        estimate_minutes=20,
        drain="low",
        deadline="friday",
        pattern_id=pattern,
        blocked_by=[blocker],
        blocks=[waiting],
        sources={"estimate_minutes": "user"},
    )
    child = make(orchard, "Find the address book", parent_id=source)
    since = last_event_id(orchard)

    task = orchard.merge_tasks(source, into)["task"]

    assert task["id"] == into
    assert task["priority"] == "low"  # the target's own value stays
    assert task["fields"]["estimate_minutes"] == {"value": 20, "source": "user"}
    assert task["drain"] == "low"
    assert task["fields"]["deadline"]["value"].startswith("2026-03-20")
    assert task["pattern"]["id"] == pattern
    assert task["notes"] == (
        f"Merged from #{source}: buy stamps\nSecond class.\nPost office closes at 5."
    )
    assert [c["id"] for c in task["children"]] == [child]
    assert task["achievable"] == "no"  # it has a child now
    assert [b["id"] for b in task["blocked_by"]] == [blocker]
    assert orchard.get_task(waiting)["task"]["blocked_by"][0]["id"] == into
    gone = orchard.get_task(source)["task"]
    assert gone["status"] == "dropped"
    assert "blocked_by" not in gone and "children" not in gone

    written = events(orchard, since)
    assert [(kind, task_id) for kind, task_id, _ in written] == [
        ("merged", source),
        ("updated", into),
    ]
    merged = written[0][2]
    assert merged == {
        "into": into,
        "moved_children": [child],
        "moved_links": 2,
        "filled": ["deadline", "estimate_minutes", "drain", "achievable", "pattern_id"],
    }
    changes = written[1][2]["changes"]
    assert changes["estimate_minutes"] == {"from": None, "to": 20, "source": "user"}
    assert changes["achievable"]["to"] == "no"
    assert changes["notes"] == {"from_chars": 0, "to_chars": len(task["notes"])}
    assert changes["pattern_id"] == {"from": None, "to": pattern}


def test_duplicate_and_self_links_are_discarded(orchard: Orchard) -> None:
    shared = make(orchard, "Shared blocker")
    into = make(orchard, "Into", blocked_by=[shared])
    source = make(orchard, "Source", blocked_by=[shared, into])  # into blocks source
    result = orchard.merge_tasks(source, into)
    assert [b["id"] for b in result["task"]["blocked_by"]] == [shared]
    assert events(orchard)[-2][2]["moved_links"] == 0


def test_a_merge_with_nothing_to_fill_writes_only_merged(orchard: Orchard) -> None:
    into, source = make(orchard, "Same"), make(orchard, "Same")
    orchard.update_task(source, notes="")
    since = last_event_id(orchard)
    orchard.merge_tasks(source, into)
    kinds = [e[0] for e in events(orchard, since)]
    assert kinds == ["merged", "updated"]  # the merged-from line is always added to notes


def test_long_notes_are_cut_to_fit(orchard: Orchard) -> None:
    into = make(orchard, "Into", notes="x" * (NOTES_MAX_CHARS - 100))
    source = make(orchard, "Source", notes="y" * 5_000)
    orchard.merge_tasks(source, into)
    with orchard.tx() as tx:
        notes = tx.session.get(Task, into).notes
    assert len(notes) == NOTES_MAX_CHARS
    assert f"see #{source}" in notes


def test_refusals_write_nothing(orchard: Orchard) -> None:
    a, b = make(orchard, "A"), make(orchard, "B")
    parent = make(orchard, "Parent")
    child = make(orchard, "Child", parent_id=parent)
    done = make(orchard, "Done")
    orchard.complete_task(done)
    laundry = orchard.create_task("Laundry", recurrence={"mode": "flexible"})["task"]["id"]
    instance = orchard.create_task(parent_id=laundry)["task"]["id"]
    before = counts(orchard)
    with pytest.raises(Invalid):
        orchard.merge_tasks(a, a)
    with pytest.raises(NotOpen):
        orchard.merge_tasks(done, a)
    with pytest.raises(NotOpen):
        orchard.merge_tasks(a, done)
    for pair in ((laundry, a), (a, laundry), (instance, b), (b, instance)):
        with pytest.raises(Unsupported):
            orchard.merge_tasks(*pair)
    for pair in ((parent, child), (child, parent)):
        with pytest.raises(Invalid):
            orchard.merge_tasks(*pair)
    assert counts(orchard) == before


def test_a_merge_that_would_make_a_loop_is_refused(orchard: Orchard) -> None:
    # into waits on x; x is a child of source. Moving x under into would make
    # into wait on its own child.
    source = make(orchard, "Source")
    x = make(orchard, "X", parent_id=source)
    into = make(orchard, "Into", blocked_by=[x])
    before = counts(orchard)
    with pytest.raises(CycleError):
        orchard.merge_tasks(source, into)
    assert counts(orchard) == before


def test_merged_sources_show_in_recent_activity(orchard: Orchard) -> None:
    into, source = make(orchard, "Into"), make(orchard, "Source")
    orchard.merge_tasks(source, into)
    [item] = orchard.get_recent_activity(days=1)["dropped"]
    assert item["id"] == source and item["reason"] == f"merged into #{into}"
