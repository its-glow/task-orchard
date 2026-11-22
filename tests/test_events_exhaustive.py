"""SPEC 6.4: every mutating operation writes exactly its events, in its own transaction.

Each case sets up what it needs, runs one call, and lists the events that call
must write as (type, task) pairs, where task is a name from the setup or None.
Payload shapes are checked in the module for each operation; here the point is
that the list is exhaustive and nothing else is written.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from task_orchard.core.clock import FixedClock
from task_orchard.core.errors import OrchardError
from task_orchard.core.service import Orchard
from task_orchard.db.models import Event
from tests.helpers import counts, last_event_id, make

Ids = dict[str, int]
Case = tuple[Callable[[Orchard], Ids], Callable[[Orchard, Ids], Any], list[tuple[str, str | None]]]


def _pattern(o: Orchard, name: str) -> int:
    return o.save_pattern(name=name)["pattern"]["id"]


def _parent_and_child(o: Orchard) -> Ids:
    parent = make(o, "Parent")
    return {"parent": parent, "child": make(o, "Child", parent_id=parent)}


def _done(o: Orchard) -> Ids:
    task = make(o)
    o.complete_task(task)
    return {"task": task}


def _capture(o: Orchard, t: Ids) -> None:
    captured = o.capture_tasks([{"title": "A"}, {"title": "B"}])["captured"]
    t.update(a=captured[0]["id"], b=captured[1]["id"])


def _subdivide(o: Orchard, t: Ids) -> None:
    children = o.subdivide_task(t["parent"], [{"title": "A"}, {"title": "B"}], sequential=True)
    t.update(a=children["children"][0]["id"], b=children["children"][1]["id"])


def _complete_untracked(o: Orchard, t: Ids) -> None:
    result = o.complete_task(title="Done", parent_id=t["parent"], new_pattern="Fresh")
    t["new"] = result["completed"]["id"]


def _blocker_and_blocked(o: Orchard) -> Ids:
    blocker = make(o, "Blocker")
    return {"blocker": blocker, "blocked": make(o, "Blocked", blocked_by=[blocker])}


CASES: dict[str, Case] = {
    "capture_tasks": (
        lambda o: {},
        _capture,
        [("created", "a"), ("created", "b")],
    ),
    "create_task": (
        lambda o: {"parent": make(o, "Parent"), "other": make(o, "Other")},
        lambda o, t: t.update(new=make(o, "New", parent_id=t["parent"], blocked_by=[t["other"]])),
        [("created", "new"), ("linked", "new"), ("updated", "parent")],
    ),
    "update_task": (
        lambda o: {"task": make(o, priority="low", sources={"priority": "user"})},
        lambda o, t: o.update_task(
            t["task"], title="Renamed", priority="high", sources={"priority": "pattern"}
        ),
        [("updated", "task"), ("conflict", "task")],
    ),
    "update_task move": (
        lambda o: {"task": make(o), "parent": make(o, "Parent")},
        lambda o, t: o.update_task(t["task"], parent_id=t["parent"]),
        [("updated", "task"), ("updated", "parent")],
    ),
    "update_task nothing": (
        lambda o: {"task": make(o, "Same")},
        lambda o, t: o.update_task(t["task"], title="Same"),
        [],
    ),
    "subdivide_task": (
        lambda o: {"parent": make(o)},
        _subdivide,
        [
            ("created", "a"),
            ("created", "b"),
            ("linked", "b"),
            ("subdivided", "parent"),
            ("updated", "parent"),
        ],
    ),
    "link_tasks": (
        lambda o: {"a": make(o), "b": make(o)},
        lambda o, t: o.link_tasks(t["a"], t["b"]),
        [("linked", "b")],
    ),
    "link_tasks again": (
        _blocker_and_blocked,
        lambda o, t: o.link_tasks(t["blocker"], t["blocked"]),
        [],
    ),
    "unlink_tasks": (
        _blocker_and_blocked,
        lambda o, t: o.unlink_tasks(t["blocker"], t["blocked"]),
        [("unlinked", "blocked")],
    ),
    "unlink_tasks missing": (
        lambda o: {"a": make(o), "b": make(o)},
        lambda o, t: o.unlink_tasks(t["a"], t["b"]),
        [],
    ),
    "start_task": (
        lambda o: {"task": make(o)},
        lambda o, t: o.start_task(t["task"]),
        [("started", "task")],
    ),
    "complete_task": (
        lambda o: {"task": make(o)},
        lambda o, t: o.complete_task(t["task"]),
        [("completed", "task")],
    ),
    "complete_task untracked": (
        lambda o: {"parent": make(o, "Parent")},
        _complete_untracked,
        [("created", "new"), ("updated", "parent"), ("pattern_saved", None), ("completed", "new")],
    ),
    "complete_task drop_open_children": (
        _parent_and_child,
        lambda o, t: o.complete_task(t["parent"], drop_open_children=True),
        [("dropped", "child"), ("completed", "parent")],
    ),
    "snooze_task": (
        lambda o: {"task": make(o)},
        lambda o, t: o.snooze_task(t["task"], days=2),
        [("snoozed", "task")],
    ),
    "snooze_task wake": (
        lambda o: {"task": make(o, snooze_until="+3d")},
        lambda o, t: o.snooze_task(t["task"], days=0),
        [("woken", "task")],
    ),
    "snooze_task wake awake": (
        lambda o: {"task": make(o)},
        lambda o, t: o.snooze_task(t["task"], days=0),
        [],
    ),
    "drop_tasks": (
        _parent_and_child,
        lambda o, t: o.drop_tasks([t["parent"]], reason="x"),
        [("dropped", "parent"), ("dropped", "child")],
    ),
    "reopen_task": (
        _done,
        lambda o, t: o.reopen_task(t["task"]),
        [("reopened", "task")],
    ),
    "save_pattern": (
        lambda o: {},
        lambda o, t: o.save_pattern(name="New"),
        [("pattern_saved", None)],
    ),
    "save_pattern merge": (
        lambda o: {"keep": _pattern(o, "Keep"), "fold": _pattern(o, "Fold")},
        lambda o, t: o.save_pattern(t["keep"], merge_from_id=t["fold"]),
        [("patterns_merged", None)],
    ),
    "save_pattern unchanged": (
        lambda o: {"keep": _pattern(o, "Keep")},
        lambda o, t: o.save_pattern(t["keep"], name="Keep"),
        [],
    ),
    "write_profile_note": (
        lambda o: {},
        lambda o, t: o.write_profile_note("energy", "Low after 3pm."),
        [("profile_saved", None)],
    ),
}


@pytest.mark.parametrize("name", CASES)
def test_each_operation_writes_exactly_its_events(
    orchard: Orchard, clock: FixedClock, name: str
) -> None:
    setup, call, expected = CASES[name]
    ids = setup(orchard)
    clock.advance(minutes=5)
    since = last_event_id(orchard)
    call(orchard, ids)
    with orchard.tx() as tx:
        written = list(tx.session.scalars(sa.select(Event).where(Event.id > since)))
    by_id = {v: k for k, v in ids.items()}
    assert [(e.type.value, by_id.get(e.task_id)) for e in written] == expected
    # All written in the call's transaction, at its `now`.
    assert all(e.at == clock.now() for e in written)


FAILURES: dict[str, tuple[Callable[[Orchard], Ids], Callable[[Orchard, Ids], Any]]] = {
    "complete_task with open children": (
        _parent_and_child,
        lambda o, t: o.complete_task(t["parent"], new_pattern="Never saved"),
    ),
    "complete_task untracked under a closed parent": (
        lambda o: {"parent": o.complete_task(title="Closed")["completed"]["id"]},
        lambda o, t: o.complete_task(title="X", parent_id=t["parent"]),
    ),
    "subdivide_task with a loop": (
        lambda o: {"parent": make(o)},
        lambda o, t: o.subdivide_task(
            t["parent"], [{"title": "A", "after": [2]}, {"title": "B", "after": [1]}]
        ),
    ),
    "drop_tasks with an unknown id": (
        lambda o: {"task": make(o)},
        lambda o, t: o.drop_tasks([t["task"], 9999], reason="x"),
    ),
    "save_pattern merge onto a taken name": (
        lambda o: {"keep": _pattern(o, "Keep"), "fold": _pattern(o, "Fold"), "x": _pattern(o, "X")},
        lambda o, t: o.save_pattern(t["keep"], merge_from_id=t["fold"], name="X"),
    ),
}


@pytest.mark.parametrize("name", FAILURES)
def test_a_failed_call_writes_nothing(orchard: Orchard, name: str) -> None:
    setup, call = FAILURES[name]
    ids = setup(orchard)
    before = counts(orchard)
    with pytest.raises(OrchardError):
        call(orchard, ids)
    assert counts(orchard) == before


def test_events_carry_the_time_of_the_call(orchard: Orchard, clock: FixedClock) -> None:
    task = make(orchard)
    clock.advance(timedelta(hours=3))
    orchard.start_task(task)
    with orchard.tx() as tx:
        latest = tx.session.scalar(sa.select(Event).order_by(Event.id.desc()).limit(1))
    assert latest.at == clock.now()
