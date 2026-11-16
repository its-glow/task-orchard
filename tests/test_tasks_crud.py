"""SPEC 8.1, 8.2, 8.3, 8.5 with 4.5 (field sources) and 6.4 (events)."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from task_orchard.core.enums import EventType, Status
from task_orchard.core.errors import (
    CycleError,
    Invalid,
    NotFound,
    NotOpen,
    OrchardError,
    ParentClosed,
    Unsupported,
)
from task_orchard.core.service import Orchard
from task_orchard.db.models import Event, Task

# --- Helpers ---


def make(orchard: Orchard, title: str = "Task", **kw: Any) -> int:
    return orchard.create_task(title=title, **kw)["task"]["id"]


def events(orchard: Orchard, since: int = 0) -> list[tuple[str, int | None, dict]]:
    with orchard.tx() as tx:
        rows = tx.session.scalars(sa.select(Event).where(Event.id > since).order_by(Event.id))
        return [(e.type.value, e.task_id, e.payload) for e in rows]


def last_event_id(orchard: Orchard) -> int:
    with orchard.tx() as tx:
        return tx.session.scalar(sa.select(sa.func.max(Event.id))) or 0


def counts(orchard: Orchard) -> tuple[int, int]:
    with orchard.tx() as tx:
        s = tx.session
        return (
            s.scalar(sa.select(sa.func.count()).select_from(Task)),
            s.scalar(sa.select(sa.func.count()).select_from(Event)),
        )


def close(orchard: Orchard, task_id: int, status: Status = Status.DONE) -> None:
    """Close a task directly, without the events a Phase 3 tool would write."""
    with orchard.tx() as tx:
        task = tx.session.get(Task, task_id)
        task.status, task.closed_at = status, tx.now


# --- capture_tasks ---


def test_capture_puts_items_in_the_inbox(orchard: Orchard) -> None:
    result = orchard.capture_tasks([{"title": "Call the dentist"}, {"title": "Desk", "notes": "x"}])
    assert [c["title"] for c in result["captured"]] == ["Call the dentist", "Desk"]
    assert "possible_duplicates" not in result
    task = orchard.get_task(result["captured"][0]["id"])["task"]
    assert task["state"] == ["inbox"]
    assert [e[0] for e in events(orchard)] == ["created", "created"]
    assert events(orchard)[0][2] == {"via": "capture"}


def test_capture_reports_possible_duplicates(orchard: Orchard) -> None:
    existing = make(orchard, "Call the dentist")
    result = orchard.capture_tasks(
        [{"title": "call the  Dentist!"}, {"title": "Buy milk"}, {"title": "buy milk"}]
    )
    new = [c["id"] for c in result["captured"]]
    assert result["possible_duplicates"] == [
        {"new_id": new[0], "existing": {"id": existing, "title": "Call the dentist"}},
        {"new_id": new[2], "existing": {"id": new[1], "title": "Buy milk"}},
    ]


@pytest.mark.parametrize("items", [[], [{"title": " "}], [{"title": f"t{n}"} for n in range(21)]])
def test_capture_validates(orchard: Orchard, items: list) -> None:
    with pytest.raises(Invalid):
        orchard.capture_tasks(items)
    assert counts(orchard) == (0, 0)


# --- create_task ---


def test_create_returns_the_detail_with_sources(orchard: Orchard) -> None:
    task = orchard.create_task(
        title="  Write   the spec ",
        priority="high",
        deadline="friday",
        estimate_minutes=90,
        activation="medium",
        motive="must",
        conditions=["At Home", "at home", " good weather "],
        sources={"deadline": "user"},
    )["task"]
    assert task["title"] == "Write the spec"
    assert task["priority"] == "high"
    assert task["due"]["at"] == "2026-03-20T23:59"
    assert task["due"]["firmness"] == "soft"
    assert task["conditions"] == ["at-home", "good-weather"]
    assert task["achievable"] == "unknown"
    assert "state" not in task  # triaged and free
    assert task["fields"]["deadline"] == {
        "value": "2026-03-20T23:59",
        "firmness": "soft",
        "source": "user",
    }
    assert task["fields"]["priority"] == {"value": "high", "source": "guess"}
    assert task["fields"]["motive"]["value"] == "must"


def test_enum_names_are_matched_leniently(orchard: Orchard) -> None:
    task = orchard.create_task(
        title="T", motive="Must", priority=" high ", firmness="HARD", deadline="+2d",
        achievable=" Yes", sources={"Estimate-Minutes": "USER"}, estimate_minutes=5,
    )["task"]  # fmt: skip
    assert (task["motive"], task["priority"], task["due"]["firmness"]) == ("must", "high", "hard")
    assert "achievable" not in task  # yes is omitted
    assert task["fields"]["estimate_minutes"]["source"] == "user"
    with pytest.raises(Invalid) as caught:
        orchard.create_task(title="T", priority="urgent")
    assert "low, normal, high, critical" in caught.value.hint


@pytest.mark.parametrize(
    "kw",
    [
        {"title": ""},
        {"title": "x" * 501},
        {"title": "T", "firmness": "hard"},
        {"title": "T", "estimate_minutes": 0},
        {"title": "T", "deadline": "next week"},
        {"title": "T", "notes": "x" * 20_001},
        {"title": "T", "sources": {"estimate": "user"}},
        {"title": "T", "conditions": "outdoors"},
    ],
)
def test_create_validates(orchard: Orchard, kw: dict) -> None:
    with pytest.raises(Invalid):
        orchard.create_task(**kw)
    assert counts(orchard) == (0, 0)


def test_create_under_a_parent_flips_its_achievable(orchard: Orchard) -> None:
    parent = make(orchard, "Parent", achievable="yes", sources={"achievable": "user"})
    mark = last_event_id(orchard)
    blocker = make(orchard, "Blocker")
    mark = last_event_id(orchard)
    child = make(orchard, "Child", parent_id=parent, blocked_by=[blocker])
    assert events(orchard, mark) == [
        ("created", child, {"via": "create", "parent_id": parent}),
        ("linked", child, {"blocker_id": blocker, "blocked_id": child}),
        (
            "updated",
            parent,
            {"changes": {"achievable": {"from": "yes", "to": "no", "source": "evidence"}}},
        ),
    ]
    detail = orchard.get_task(parent)["task"]
    assert detail["achievable"] == "no"
    assert detail["fields"]["achievable"] == {"value": "no", "source": "evidence"}
    # Already no: no further event.
    mark = last_event_id(orchard)
    make(orchard, "Second child", parent_id=parent)
    assert [e[0] for e in events(orchard, mark)] == ["created"]


def test_create_parent_rules(orchard: Orchard) -> None:
    closed = make(orchard, "Closed")
    close(orchard, closed)
    with pytest.raises(ParentClosed):
        make(orchard, "Child", parent_id=closed)
    with pytest.raises(NotFound):
        make(orchard, "Child", parent_id=999)


def test_create_links_must_be_open(orchard: Orchard) -> None:
    done = make(orchard, "Done")
    close(orchard, done)
    with pytest.raises(NotOpen) as caught:
        make(orchard, "T", blocked_by=[done])
    assert "reopen_task" in caught.value.hint
    with pytest.raises(NotOpen):
        make(orchard, "T", blocks=[done])


def test_a_failed_call_writes_nothing(orchard: Orchard) -> None:
    a = make(orchard, "A")
    b = make(orchard, "B", blocked_by=[a])
    before = counts(orchard)
    with pytest.raises(CycleError):
        make(orchard, "C", blocked_by=[b], blocks=[a])
    assert counts(orchard) == before
    with pytest.raises(OrchardError):
        orchard.update_task(a, priority="high", parent_id=b)
    assert counts(orchard) == before
    assert "priority" not in orchard.get_task(a)["task"]


# --- update_task ---


def test_update_and_clear(orchard: Orchard) -> None:
    task = make(orchard, "T", priority="low", estimate_minutes=30)
    mark = last_event_id(orchard)
    detail = orchard.update_task(
        task, title="Renamed", priority="high", clear=["estimate_minutes"], drain="low"
    )["task"]
    assert detail["title"] == "Renamed"
    assert detail["priority"] == "high"
    assert "estimate_minutes" not in detail and "estimate_minutes" not in detail.get("fields", {})
    [(kind, task_id, payload)] = events(orchard, mark)
    assert (kind, task_id) == ("updated", task)
    assert payload["changes"] == {
        "title": {"from": "T", "to": "Renamed"},
        "priority": {"from": "low", "to": "high", "source": "guess"},
        "drain": {"from": None, "to": "low", "source": "guess"},
        "estimate_minutes": {"from": 30, "to": None},
    }


def test_a_call_that_changes_nothing_writes_no_event(orchard: Orchard) -> None:
    task = make(orchard, "T", priority="low")
    mark = last_event_id(orchard)
    orchard.update_task(task, priority="low", title="T")
    assert events(orchard, mark) == []


def test_confirming_a_guess_records_the_new_source(orchard: Orchard) -> None:
    task = make(orchard, "T", priority="low")
    mark = last_event_id(orchard)
    orchard.update_task(task, priority="low", sources={"priority": "user"})
    [(_, _, payload)] = events(orchard, mark)
    assert payload["changes"]["priority"] == {"from": "low", "to": "low", "source": "user"}


def test_update_takes_a_task_out_of_the_inbox(orchard: Orchard) -> None:
    [captured] = orchard.capture_tasks([{"title": "Captured"}])["captured"]
    mark = last_event_id(orchard)
    detail = orchard.update_task(captured["id"])["task"]
    assert "state" not in detail
    assert events(orchard, mark)[0][2]["changes"] == {"triaged": {"from": False, "to": True}}
    detail = orchard.update_task(captured["id"], priority="low", triaged=False)["task"]
    assert detail["state"] == ["inbox"]


def test_setting_and_clearing_one_field_is_invalid(orchard: Orchard) -> None:
    task = make(orchard, "T")
    with pytest.raises(Invalid):
        orchard.update_task(task, priority="low", clear=["priority"])
    with pytest.raises(Invalid):
        orchard.update_task(task, clear=["title"])
    with pytest.raises(Invalid):
        orchard.update_task(task, notes="x", append_note="y")


def test_clear_takes_parent_id_and_pattern_id(orchard: Orchard, seeded: dict[str, int]) -> None:
    spec = seeded["obs_spec"]
    detail = orchard.get_task(spec)["task"]
    assert detail["parent"]["id"] == seeded["observability"]
    assert "pattern" in detail
    detail = orchard.update_task(spec, clear=["parent_id", "pattern_id"])["task"]
    assert "parent" not in detail and "pattern" not in detail
    assert "path" not in detail


def test_move(orchard: Orchard) -> None:
    a, b = make(orchard, "A"), make(orchard, "B", achievable="yes")
    child = make(orchard, "Child", parent_id=a)
    mark = last_event_id(orchard)
    detail = orchard.update_task(child, parent_id=b)["task"]
    assert detail["parent"]["id"] == b
    assert events(orchard, mark) == [
        ("updated", child, {"changes": {"parent_id": {"from": a, "to": b}}}),
        (
            "updated",
            b,
            {"changes": {"achievable": {"from": "yes", "to": "no", "source": "evidence"}}},
        ),
    ]
    closed = make(orchard, "Closed")
    close(orchard, closed)
    with pytest.raises(ParentClosed):
        orchard.update_task(child, parent_id=closed)


# --- Deadline and firmness ---


def test_firmness_alone_regrades_the_deadline(orchard: Orchard) -> None:
    task = make(orchard, "T", deadline="friday")
    detail = orchard.update_task(task, firmness="hard")["task"]
    assert (detail["due"]["at"], detail["due"]["firmness"]) == ("2026-03-20T23:59", "hard")
    detail = orchard.update_task(task, deadline="2026-03-25")["task"]
    assert (detail["due"]["at"], detail["due"]["firmness"]) == ("2026-03-25T23:59", "hard")
    detail = orchard.update_task(task, clear=["deadline"])["task"]
    assert "due" not in detail and "deadline" not in detail.get("fields", {})
    detail = orchard.update_task(task, deadline="2026-03-26")["task"]
    assert detail["due"]["firmness"] == "soft"


def test_firmness_alone_without_a_deadline_is_invalid(orchard: Orchard) -> None:
    task = make(orchard, "T")
    with pytest.raises(Invalid):
        orchard.update_task(task, firmness="hard")


# --- Protection (4.5) ---


def test_user_values_are_protected(orchard: Orchard) -> None:
    task = make(orchard, "T", priority="low", estimate_minutes=30, sources={"priority": "user"})
    mark = last_event_id(orchard)
    result = orchard.update_task(task, priority="high", estimate_minutes=45)
    assert result["task"]["priority"] == "low"
    assert result["task"]["estimate_minutes"] == 45
    assert result["skipped"] == [
        {"field": "priority", "kept": "low", "proposed": "high", "reason": "set by the user"}
    ]
    assert 'sources={"priority": "user"}' in result["hint"]
    # A guess that is skipped writes no conflict.
    assert [e[0] for e in events(orchard, mark)] == ["updated"]

    # The user's own change goes through.
    result = orchard.update_task(task, priority="high", sources={"priority": "user"})
    assert result["task"]["priority"] == "high"
    assert "skipped" not in result and "hint" not in result

    # Every skipped field is named, so one retry is enough.
    orchard.update_task(task, deadline="friday", sources={"deadline": "user"})
    both = orchard.update_task(task, priority="normal", clear=["deadline"])
    assert [s["field"] for s in both["skipped"]] == ["priority", "deadline"]
    assert 'sources={"priority": "user", "deadline": "user"}' in both["hint"]


def test_pattern_or_evidence_that_disagrees_is_a_conflict(orchard: Orchard) -> None:
    task = make(orchard, "T", estimate_minutes=30, sources={"estimate_minutes": "user"})
    mark = last_event_id(orchard)
    result = orchard.update_task(task, estimate_minutes=60, sources={"estimate_minutes": "pattern"})
    assert result["skipped"][0]["field"] == "estimate_minutes"
    assert events(orchard, mark) == [
        (
            "conflict",
            task,
            {"field": "estimate_minutes", "kept": 30, "proposed": 60, "source": "pattern"},
        )
    ]


def test_clearing_is_protected(orchard: Orchard) -> None:
    task = make(orchard, "T", deadline="friday", firmness="hard", sources={"deadline": "user"})
    result = orchard.update_task(task, clear=["deadline"])
    assert result["skipped"] == [
        {
            "field": "deadline",
            "kept": {"at": "2026-03-20T23:59", "firmness": "hard"},
            "proposed": None,
            "reason": "set by the user",
        }
    ]
    assert "hint" in result
    assert result["task"]["due"]["firmness"] == "hard"
    # Firmness alone is a write to `deadline`, so it is protected too.
    assert orchard.update_task(task, firmness="soft")["skipped"][0]["field"] == "deadline"

    result = orchard.update_task(task, clear=["deadline"], sources={"deadline": "user"})
    assert "due" not in result["task"]
    assert "deadline" not in result["task"].get("fields", {})


def test_the_parent_flip_ignores_protection(orchard: Orchard) -> None:
    parent = make(orchard, "P", achievable="yes", sources={"achievable": "user"})
    make(orchard, "Child", parent_id=parent)
    assert orchard.get_task(parent)["task"]["fields"]["achievable"]["source"] == "evidence"


# --- Achievable and children ---


def test_achievable_yes_is_refused_on_a_task_with_children(orchard: Orchard) -> None:
    parent = make(orchard, "P")
    child = make(orchard, "C", parent_id=parent)
    with pytest.raises(Invalid) as caught:
        orchard.update_task(parent, achievable="yes", sources={"achievable": "user"})
    assert "not one step" in caught.value.hint
    close(orchard, child, Status.DONE)
    with pytest.raises(Invalid):
        orchard.update_task(parent, achievable="yes")
    with orchard.tx() as tx:  # a breakdown that was abandoned does not lock the task
        tx.session.get(Task, child).status = Status.DROPPED
    assert "achievable" not in orchard.update_task(parent, achievable="yes")["task"]


# --- Closed tasks ---


def test_closed_tasks_take_only_notes_description_and_pattern(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    done = seeded["obs_backend"]
    detail = orchard.update_task(done, append_note="Was harder than it looked.")["task"]
    assert detail["notes"].endswith("Was harder than it looked.")
    assert detail["status"] == "done"
    for kw in ({"priority": "high"}, {"title": "x"}, {"clear": ["deadline"]}):
        with pytest.raises(NotOpen) as caught:
            orchard.update_task(done, **kw)
        assert "reopen_task" in caught.value.hint


def test_notes_are_capped(orchard: Orchard) -> None:
    task = make(orchard, "T", notes="x" * 19_990)
    with pytest.raises(Invalid) as caught:
        orchard.update_task(task, append_note="y" * 20)
    assert "update_task(notes=" in caught.value.hint
    orchard.update_task(task, append_note="y" * 5)


def test_text_changes_are_logged_by_length(orchard: Orchard) -> None:
    task = make(orchard, "T", notes="abc")
    mark = last_event_id(orchard)
    orchard.update_task(task, append_note="de")
    assert events(orchard, mark)[0][2]["changes"] == {"notes": {"from_chars": 3, "to_chars": 6}}


# --- Recurring tasks before Phase 4 ---


def test_permanent_recurring_rules(orchard: Orchard, seeded: dict[str, int]) -> None:
    laundry, summary = seeded["laundry"], seeded["summary"]
    assert orchard.update_task(laundry, priority="high")["task"]["priority"] == "high"
    with pytest.raises(Unsupported):  # the server spawns a scheduled task's occurrences
        make(orchard, "Extra summary", parent_id=summary)
    with pytest.raises(Unsupported):
        make(orchard, "T", blocked_by=[summary])
    with pytest.raises(Unsupported):
        orchard.link_tasks(blocker_id=laundry, blocked_id=seeded["cert"])
    with pytest.raises(Unsupported):
        orchard.update_task(seeded["cert"], parent_id=laundry)
    with pytest.raises(Unsupported):  # an instance cannot be moved
        orchard.update_task(seeded["summary_w3"], clear=["parent_id"])
    # Being blocked is allowed.
    result = orchard.link_tasks(blocker_id=seeded["cert"], blocked_id=laundry)
    assert result["blocked"]["blocked_by"][0]["id"] == seeded["cert"]
    # An instance is an ordinary task.
    assert orchard.update_task(seeded["summary_w3"], priority="high")["task"]["priority"] == "high"


# --- link_tasks, unlink_tasks ---


def test_link_and_unlink(orchard: Orchard) -> None:
    a, b = make(orchard, "A"), make(orchard, "B")
    mark = last_event_id(orchard)
    result = orchard.link_tasks(blocker_id=a, blocked_id=b)
    assert result["blocker"]["unblocks"] == 1
    assert result["blocked"]["state"] == ["blocked"]
    assert "already_linked" not in result
    assert orchard.link_tasks(blocker_id=a, blocked_id=b)["already_linked"] is True
    assert orchard.unlink_tasks(blocker_id=a, blocked_id=b)["blocked"]["id"] == b
    assert orchard.unlink_tasks(blocker_id=a, blocked_id=b)["was_linked"] is False
    assert events(orchard, mark) == [
        ("linked", b, {"blocker_id": a, "blocked_id": b}),
        ("unlinked", b, {"blocker_id": a, "blocked_id": b}),
    ]
    with pytest.raises(Invalid):
        orchard.link_tasks(blocker_id=a, blocked_id=a)
    with pytest.raises(NotFound):
        orchard.link_tasks(blocker_id=a, blocked_id=999)


def test_events_written_by_create_are_typed(orchard: Orchard) -> None:
    make(orchard, "T")
    with orchard.tx() as tx:
        assert tx.session.scalar(sa.select(Event.type)) is EventType.CREATED


def test_conditions_are_short_and_few(orchard: Orchard) -> None:
    five = ["a", "b", "c", "d", "e"]
    assert len(orchard.create_task("T", conditions=five)["task"]["conditions"]) == 5
    with pytest.raises(Invalid):
        orchard.create_task("T", conditions=[*five, "f"])
    with pytest.raises(Invalid):
        orchard.create_task("T", conditions=["x" * 31])
