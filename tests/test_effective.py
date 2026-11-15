"""SPEC 5.4: effective priority, deadline (any and hard) and motive."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from task_orchard.core.derive import Snapshot
from task_orchard.core.enums import Priority, Status
from task_orchard.core.errors import OrchardError
from task_orchard.core.service import Orchard
from task_orchard.db.models import Event, Task, TaskBlock
from tests.conftest import TZ


def make(orchard: Orchard, title: str, **kw: Any) -> int:
    return orchard.create_task(title=title, **kw)["task"]["id"]


def row(orchard: Orchard, task_id: int) -> dict[str, Any]:
    return orchard.get_task(task_id)["task"]


def local_at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ).astimezone(UTC)


# --- Against the seed ---


def test_a_blocker_inherits_the_release(orchard: Orchard, seeded: dict[str, int]) -> None:
    cert = row(orchard, seeded["cert"])
    publish = seeded["release_publish"]
    assert cert["priority"] == "critical"
    assert cert["priority_own"] == "low"
    assert cert["priority_via"] == {
        "id": publish,
        "title": "Publish the signed build",
        "how": "blocks",
    }

    release_due = row(orchard, seeded["release"])["due"]
    assert release_due["firmness"] == "hard"
    assert cert["due"]["firmness"] == "hard"
    assert cert["due"]["via"]["id"] == publish
    gap = local_at(release_due["at"]) - local_at(cert["due"]["at"])
    assert gap == timedelta(minutes=20)  # publish's estimate
    assert "hard_due" not in cert

    orchard.unlink_tasks(blocker_id=seeded["cert"], blocked_id=publish)
    cert = row(orchard, seeded["cert"])
    assert cert["priority"] == "low"
    assert "priority_via" not in cert and "priority_own" not in cert
    assert "due" not in cert


def test_motive_comes_from_the_nearest_ancestor(orchard: Orchard, seeded: dict[str, int]) -> None:
    build = row(orchard, seeded["game_build"])
    assert build["motive"] == "want"
    assert "motive" not in build.get("fields", {})


def test_children_inherit_priority_and_deadline(orchard: Orchard, seeded: dict[str, int]) -> None:
    flaky = row(orchard, seeded["release_flaky"])
    release = seeded["release"]
    assert flaky["priority"] == "critical"
    assert flaky["priority_via"]["how"] == "parent"
    assert flaky["priority_via"]["id"] == release
    # Its due comes through the chain flaky -> notes -> publish, not from the
    # parent: the notes (45 min) and the publish (20 min) must fit after it.
    notes = row(orchard, seeded["release_notes"])
    assert notes["due"]["via"]["id"] == seeded["release_publish"]
    assert flaky["due"]["via"] == {
        "id": seeded["release_notes"],
        "title": "Write the release notes",
        "how": "blocks",
    }
    release_due = local_at(row(orchard, release)["due"]["at"])
    assert release_due - local_at(flaky["due"]["at"]) == timedelta(minutes=65)
    assert release_due - local_at(notes["due"]["at"]) == timedelta(minutes=20)
    publish = row(orchard, seeded["release_publish"])
    assert publish["due"]["via"] == {"id": release, "title": "Ship release 2.4", "how": "parent"}


# --- Priority ---


def test_priority_rules(orchard: Orchard) -> None:
    plain = make(orchard, "Plain")
    assert "priority" not in row(orchard, plain)

    top = make(orchard, "Top", priority="high")
    child = make(orchard, "Child", parent_id=top, priority="low")
    assert row(orchard, child)["priority"] == "high"
    assert row(orchard, child)["priority_own"] == "low"

    # Highest wins; ties prefer own, then parent.
    same = make(orchard, "Same", parent_id=top, priority="high")
    assert "priority_via" not in row(orchard, same)
    grandchild = make(orchard, "Grandchild", parent_id=child)
    assert row(orchard, grandchild)["priority_via"]["id"] == child

    # Through a blocked task's descendant, reported via the blocked task itself.
    project = make(orchard, "Project")
    make(orchard, "Urgent step", parent_id=project, priority="critical")
    blocker = make(orchard, "Blocker", blocks=[project])
    blocker_row = row(orchard, blocker)
    assert blocker_row["priority"] == "critical"
    assert blocker_row["priority_via"]["id"] == project


def test_closed_tasks_pass_nothing_on(orchard: Orchard) -> None:
    blocked = make(orchard, "Blocked", priority="critical", deadline="+1d")
    blocker = make(orchard, "Blocker", blocks=[blocked])
    assert row(orchard, blocker)["priority"] == "critical"
    with orchard.tx() as tx:
        t = tx.session.get(Task, blocked)
        t.status, t.closed_at = Status.DONE, tx.now
    after = row(orchard, blocker)
    assert "priority" not in after and "due" not in after
    # A closed task shows its own values.
    closed = row(orchard, blocked)
    assert closed["priority"] == "critical" and "priority_via" not in closed


# --- Deadlines ---


def test_a_blocker_must_finish_before_the_blocked_work_starts(orchard: Orchard) -> None:
    project = make(orchard, "Project", deadline="2026-03-25T17:00", firmness="hard")
    make(orchard, "Step one", parent_id=project, estimate_minutes=60)
    make(orchard, "Step two", parent_id=project, estimate_minutes=30)
    blocker = make(orchard, "Blocker", blocks=[project])
    due = row(orchard, blocker)["due"]
    assert due["at"] == "2026-03-25T15:30"  # 90 minutes of the project's work before
    assert due["firmness"] == "hard"
    assert due["via"]["how"] == "blocks"


def test_the_earliest_deadline_wins(orchard: Orchard) -> None:
    parent = make(orchard, "Parent", deadline="2026-03-25")
    early = make(orchard, "Early", parent_id=parent, deadline="2026-03-20")
    late = make(orchard, "Late", parent_id=parent, deadline="2026-03-30")
    assert row(orchard, early)["due"]["at"] == "2026-03-20T23:59"
    assert "via" not in row(orchard, early)["due"]
    assert row(orchard, late)["due"]["at"] == "2026-03-25T23:59"
    assert row(orchard, late)["due"]["via"]["id"] == parent


def test_hard_due_only_when_later_than_due(orchard: Orchard) -> None:
    project = make(orchard, "Project", deadline="2026-03-30", firmness="hard")
    soft_early = make(orchard, "Soft early", parent_id=project, deadline="2026-03-20")
    task = row(orchard, soft_early)
    assert (task["due"]["at"], task["due"]["firmness"]) == ("2026-03-20T23:59", "soft")
    assert task["hard_due"]["at"] == "2026-03-30T23:59"
    assert task["hard_due"]["firmness"] == "hard"
    assert task["hard_due"]["via"]["id"] == project

    hard_early = make(
        orchard, "Hard early", parent_id=project, deadline="2026-03-20", firmness="hard"
    )
    assert "hard_due" not in row(orchard, hard_early)
    soft_parent = make(orchard, "Soft parent", deadline="2026-03-30")
    hard_child = make(
        orchard, "Hard child", parent_id=soft_parent, deadline="2026-03-20", firmness="hard"
    )
    assert "hard_due" not in row(orchard, hard_child)


def test_the_hard_pass_uses_hard_deadlines_through_links(orchard: Orchard) -> None:
    soft = make(orchard, "Soft target", deadline="2026-03-20", estimate_minutes=60)
    hard = make(
        orchard, "Hard target", deadline="2026-03-27T12:00", firmness="hard", estimate_minutes=30
    )
    blocker = make(orchard, "Blocker", blocks=[soft, hard])
    task = row(orchard, blocker)
    assert task["due"]["at"] == "2026-03-20T22:59"
    assert task["hard_due"]["at"] == "2026-03-27T11:30"
    assert task["hard_due"]["via"]["id"] == hard


# --- Motive ---


def test_motive_does_not_travel_through_links(orchard: Orchard) -> None:
    wanted = make(orchard, "Wanted", motive="want")
    blocker = make(orchard, "Blocker", blocks=[wanted])
    assert "motive" not in row(orchard, blocker)
    child = make(orchard, "Child", parent_id=wanted)
    grandchild = make(orchard, "Grandchild", parent_id=child)
    assert row(orchard, grandchild)["motive"] == "want"
    own = make(orchard, "Own", parent_id=wanted, motive="must")
    assert row(orchard, own)["motive"] == "must"


# --- Changes take effect on the next read ---


def test_moving_changes_effective_values_at_once(orchard: Orchard) -> None:
    urgent = make(orchard, "Urgent", priority="critical", deadline="+2d", motive="must")
    calm = make(orchard, "Calm")
    task = make(orchard, "Task", parent_id=urgent)
    assert row(orchard, task)["priority"] == "critical"
    orchard.update_task(task, parent_id=calm)
    moved = row(orchard, task)
    assert "priority" not in moved and "due" not in moved and "motive" not in moved


# --- Property: effective priority is never below own priority ---

_op = st.one_of(
    st.tuples(st.just("create"), st.integers(0, 10), st.sampled_from([None, *Priority])),
    st.tuples(st.just("link"), st.integers(0, 10), st.integers(0, 10)),
    st.tuples(st.just("move"), st.integers(0, 10), st.integers(0, 10)),
)


@given(ops=st.lists(_op, min_size=1, max_size=15))
@settings(
    max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_effective_priority_is_never_below_own(orchard: Orchard, ops: list[tuple]) -> None:
    with orchard.tx() as tx:  # hypothesis reuses the fixture: start empty
        for model in (Event, TaskBlock):
            tx.session.query(model).delete()
        tx.session.query(Task).update({Task.parent_id: None})
        tx.session.query(Task).delete()

    ids: list[int] = []
    for kind, a, b in ops:
        pick = lambda n: ids[n % len(ids)] if ids else None  # noqa: E731
        try:
            if kind == "create":
                level = b.label if b is not None else None
                ids.append(make(orchard, f"T{len(ids)}", priority=level, parent_id=pick(a)))
            elif kind == "link" and ids:
                orchard.link_tasks(blocker_id=pick(a), blocked_id=pick(b))
            elif kind == "move" and ids:
                orchard.update_task(pick(a), parent_id=pick(b))
        except OrchardError:
            pass

    with orchard.tx() as tx:
        snap = Snapshot(tx.session, tx.now)
        for i in ids:
            task = snap.task(i)
            effective = snap.effective_priority(task)
            if task.priority is not None:
                assert effective is not None and effective.value >= task.priority
