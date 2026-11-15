"""SPEC 5.3: the predicates, computed on read from one snapshot."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from task_orchard.core.clock import FixedClock
from task_orchard.core.derive import Snapshot
from task_orchard.core.enums import Status
from task_orchard.core.errors import OrchardError
from task_orchard.core.service import Orchard
from task_orchard.db.models import Event, Task, TaskBlock


def make(orchard: Orchard, title: str, **kw: Any) -> int:
    return orchard.create_task(title=title, **kw)["task"]["id"]


def with_snap(orchard: Orchard, fn: Any) -> Any:
    with orchard.tx() as tx:
        return fn(Snapshot(tx.session, tx.now))


# --- Each predicate ---


def test_dormant_is_inherited_from_an_ancestor(orchard: Orchard, clock: FixedClock) -> None:
    top = make(orchard, "Top", snooze_until="+3d")
    child = make(orchard, "Child", parent_id=top)
    other = make(orchard, "Other")

    def check(snap: Snapshot) -> tuple[bool, bool, bool]:
        return tuple(snap.dormant(snap.task(i)) for i in (top, child, other))  # type: ignore[return-value]

    assert with_snap(orchard, check) == (True, True, False)
    clock.advance(days=4)
    assert with_snap(orchard, check) == (False, False, False)


def test_blocked_is_inherited_and_a_dormant_blocker_still_blocks(orchard: Orchard) -> None:
    blocker = make(orchard, "Blocker", snooze_until="+3d")
    project = make(orchard, "Project", blocked_by=[blocker])
    step = make(orchard, "Step", parent_id=project)

    def check(snap: Snapshot) -> Any:
        return (
            snap.blocked(snap.task(project)),
            snap.blocked(snap.task(step)),
            snap.effective_blockers(snap.task(step)),
            snap.blocked(snap.task(blocker)),
        )

    assert with_snap(orchard, check) == (True, True, [(blocker, project)], False)


def test_a_closed_blocker_does_not_block(orchard: Orchard) -> None:
    blocker = make(orchard, "Blocker")
    blocked = make(orchard, "Blocked", blocked_by=[blocker])
    with orchard.tx() as tx:
        t = tx.session.get(Task, blocker)
        t.status, t.closed_at = Status.DONE, tx.now
    assert not with_snap(orchard, lambda s: s.blocked(s.task(blocked)))


def test_has_open_children_and_candidate(orchard: Orchard) -> None:
    parent = make(orchard, "Parent")
    child = make(orchard, "Child", parent_id=parent)
    inbox = orchard.capture_tasks([{"title": "Captured"}])["captured"][0]["id"]

    def check(snap: Snapshot) -> Any:
        p, c, i = (snap.task(x) for x in (parent, child, inbox))
        return (snap.has_open_children(p), snap.candidate(p), snap.candidate(c), snap.candidate(i))

    assert with_snap(orchard, check) == (True, False, True, False)


# --- Against the seed ---


def test_seeded_predicates(orchard: Orchard, seeded: dict[str, int]) -> None:
    def check(snap: Snapshot) -> None:
        get = lambda h: snap.task(seeded[h])  # noqa: E731
        venue = get("offsite_venue")
        assert snap.blocked(venue)
        assert snap.effective_blockers(venue) == [(seeded["budget"], seeded["offsite"])]
        assert snap.dormant(get("taxes"))
        assert snap.blocked(get("release_notes"))
        assert snap.candidate(get("cert"))
        assert not snap.candidate(get("inbox_run"))
        # Recurring: a scheduled task never, its open instance may be;
        # a flexible task when awake with no open instance.
        assert not snap.candidate(get("summary"))
        assert snap.is_instance(get("summary_w3"))
        assert snap.candidate(get("laundry"))
        assert not snap.candidate(get("run"))  # still resting
        assert not snap.is_leaf(get("summary"))

    with_snap(orchard, check)


def test_seeded_rows_show_the_flags(orchard: Orchard, seeded: dict[str, int]) -> None:
    venue = orchard.get_task(seeded["offsite_venue"])["task"]
    assert "blocked" in venue["state"]
    assert venue["blocked_by"][0]["via"]["id"] == seeded["offsite"]
    assert "dormant" in orchard.get_task(seeded["taxes"])["task"]["state"]
    assert "overdue" in orchard.get_task(seeded["milk"])["task"]["state"]
    assert "inbox" in orchard.get_task(seeded["inbox_desk"])["task"]["state"]
    assert "state" not in orchard.get_task(seeded["cert"])["task"]


# --- Property: no candidate is ever blocked, dormant, untriaged or a parent ---

_op = st.one_of(
    st.tuples(st.just("create"), st.integers(0, 12), st.booleans()),
    st.tuples(st.just("capture"), st.integers(0, 12), st.booleans()),
    st.tuples(st.just("link"), st.integers(0, 12), st.integers(0, 12)),
    st.tuples(st.just("snooze"), st.integers(0, 12), st.integers(-2, 3)),
    st.tuples(st.just("move"), st.integers(0, 12), st.integers(0, 12)),
)


@given(ops=st.lists(_op, min_size=1, max_size=20))
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_no_candidate_is_ever_excluded_by_a_predicate(
    orchard: Orchard, clock: FixedClock, ops: list[tuple[str, int, Any]]
) -> None:
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
                parent = pick(a) if b else None
                ids.append(make(orchard, f"T{len(ids)}", parent_id=parent))
            elif kind == "capture":
                ids.append(orchard.capture_tasks([{"title": f"C{len(ids)}"}])["captured"][0]["id"])
            elif kind == "link" and ids:
                orchard.link_tasks(blocker_id=pick(a), blocked_id=pick(b))
            elif kind == "snooze" and ids:
                when = f"+{b}d" if b > 0 else "2020-01-01"
                orchard.update_task(pick(a), snooze_until=when, triaged=False)
            elif kind == "move" and ids:
                orchard.update_task(pick(a), parent_id=pick(b))
        except OrchardError:
            pass
        clock.advance(hours=7)

    def check(snap: Snapshot) -> None:
        for i in ids:
            task = snap.task(i)
            if snap.candidate(task):
                assert task.triaged
                assert not snap.blocked(task)
                assert not snap.dormant(task)
                assert not snap.has_open_children(task)

    with_snap(orchard, check)
    clock.set(clock.now() - timedelta(hours=7 * len(ops)))
