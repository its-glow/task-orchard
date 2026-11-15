"""SPEC 5.1 and 5.2: the must-finish-first graph and the cycle check.

The pure functions in core.graph are tested directly, including a property
test; the rejection cases are then tested through the service, which is where
the readable path (titles and statuses) is built.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from task_orchard.core import graph
from task_orchard.core.enums import Status
from task_orchard.core.errors import CycleError
from task_orchard.core.service import Orchard
from task_orchard.db.models import Task

# --- Pure functions ---


def test_edges_include_inherited_blocking() -> None:
    # 3 is a child of 2; 1 blocks 2, so 1 also comes before 3.
    edges = graph.build_edges({1: None, 2: None, 3: 2}, {(1, 2)})
    assert edges[3] == {2: graph.CHILD_OF}
    assert edges[1] == {2: graph.BLOCKS, 3: graph.BLOCKS_ANCESTOR_OF}


def test_the_most_direct_reason_wins_for_a_pair() -> None:
    # 1 blocks 2 and 3 explicitly; 3 is also below 2.
    edges = graph.build_edges({1: None, 2: None, 3: 2}, {(1, 2), (1, 3)})
    assert edges[1][3] == graph.BLOCKS


def test_find_cycle() -> None:
    assert graph.find_cycle({1: {2: "x"}, 2: {3: "x"}}) is None
    assert graph.find_cycle({1: {2: "x"}, 2: {1: "x"}}) in ([1, 2, 1], [2, 1, 2])
    assert graph.find_cycle({1: {1: "x"}}) == [1, 1]


def test_a_loop_starts_with_the_proposed_change() -> None:
    structure = graph.Structure(parent={1: None, 2: None, 3: None}, blocks={(1, 2), (2, 3)})
    steps = graph.check(structure, add_blocks=[(3, 1)])
    assert steps == [(3, "blocks"), (1, "blocks"), (2, "blocks"), (3, None)]


def test_descendants_and_ancestors() -> None:
    parent = {1: None, 2: 1, 3: 2, 4: 1}
    children = graph.children_map(parent)
    assert graph.descendants(1, children) == [2, 3, 4]
    assert graph.ancestors(3, parent) == [2, 1]
    assert graph.ancestors(1, parent) == []


# Property: accepting only the changes check() allows keeps the graph acyclic.

N = 8
_change = st.one_of(
    st.tuples(st.just("link"), st.integers(0, N - 1), st.integers(0, N - 1)),
    st.tuples(st.just("move"), st.integers(0, N - 1), st.none() | st.integers(0, N - 1)),
)


@given(st.lists(_change, max_size=40))
@settings(max_examples=150, deadline=None)
def test_accepted_changes_never_make_a_loop(changes: list[tuple[str, int, int | None]]) -> None:
    structure = graph.Structure(parent=dict.fromkeys(range(N)), blocks=set())
    for kind, a, b in changes:
        if kind == "link":
            if a == b or b is None:
                continue
            if graph.check(structure, add_blocks=[(a, b)]) is None:
                structure.blocks.add((a, b))
        elif graph.check(structure, set_parent={a: b}) is None:
            structure.parent[a] = b
        edges = graph.build_edges(structure.parent, structure.blocks)
        assert graph.find_cycle(edges) is None


# --- Through the service: each rejection case in 5.2 ---


def make(orchard: Orchard, title: str, **kw: Any) -> int:
    return orchard.create_task(title=title, **kw)["task"]["id"]


def cycle_path(caught: pytest.ExceptionInfo[CycleError]) -> list[dict[str, Any]]:
    path = caught.value.details["path"]
    for step in path:
        assert {"id", "title", "status"} <= step.keys()
    assert path[0]["id"] == path[-1]["id"]
    assert all("because" in step for step in path[:-1])
    return path


def test_mutual_blocking_is_rejected(orchard: Orchard) -> None:
    a, b = make(orchard, "A"), make(orchard, "B")
    orchard.link_tasks(blocker_id=a, blocked_id=b)
    with pytest.raises(CycleError) as caught:
        orchard.link_tasks(blocker_id=b, blocked_id=a)
    path = cycle_path(caught)
    assert [(s["id"], s.get("because")) for s in path] == [
        (b, "blocks"),
        (a, "blocks"),
        (b, None),
    ]
    assert path[0]["title"] == "B" and path[0]["status"] == "open"
    assert "unlink_tasks" in caught.value.hint


def test_blocking_through_an_ancestors_blocker_is_rejected(orchard: Orchard) -> None:
    # X blocks P; A is a child of P; A blocks X. Explicit links show no loop.
    x, p = make(orchard, "X"), make(orchard, "P")
    a = make(orchard, "A", parent_id=p)
    orchard.link_tasks(blocker_id=x, blocked_id=p)
    with pytest.raises(CycleError) as caught:
        orchard.link_tasks(blocker_id=a, blocked_id=x)
    becauses = [s.get("because") for s in cycle_path(caught)]
    assert "blocks an ancestor of" in becauses


def test_blocking_an_ancestor_or_a_descendant_is_rejected(orchard: Orchard) -> None:
    top = make(orchard, "Top")
    mid = make(orchard, "Mid", parent_id=top)
    leaf = make(orchard, "Leaf", parent_id=mid)
    with pytest.raises(CycleError):
        orchard.link_tasks(blocker_id=leaf, blocked_id=top)
    with pytest.raises(CycleError):
        orchard.link_tasks(blocker_id=top, blocked_id=leaf)


def test_moving_under_a_descendant_is_rejected(orchard: Orchard) -> None:
    top = make(orchard, "Top")
    mid = make(orchard, "Mid", parent_id=top)
    with pytest.raises(CycleError) as caught:
        orchard.update_task(top, parent_id=mid)
    assert [s.get("because") for s in cycle_path(caught)] == ["child of", "child of", None]
    with pytest.raises(CycleError):
        orchard.update_task(top, parent_id=top)


def test_moving_under_a_parent_whose_blocker_waits_on_you_is_rejected(orchard: Orchard) -> None:
    # B blocks P, and B is waiting on T. Moving T under P would make T wait on B.
    p, b, t = make(orchard, "P"), make(orchard, "B"), make(orchard, "T")
    t_child = make(orchard, "T child", parent_id=t)
    orchard.link_tasks(blocker_id=b, blocked_id=p)
    orchard.link_tasks(blocker_id=t_child, blocked_id=b)
    with pytest.raises(CycleError):
        orchard.update_task(t, parent_id=p)
    # Same through an ancestor of P.
    q = make(orchard, "Q", parent_id=p)
    with pytest.raises(CycleError):
        orchard.update_task(t, parent_id=q)


def test_creating_with_links_is_cycle_checked(orchard: Orchard) -> None:
    a = make(orchard, "A")
    b = make(orchard, "B", blocked_by=[a])
    with pytest.raises(CycleError):
        make(orchard, "C", blocked_by=[b], blocks=[a])
    with pytest.raises(CycleError):
        make(orchard, "D", parent_id=a, blocks=[a])


def test_a_loop_through_a_closed_task_is_rejected_until_unlinked(orchard: Orchard) -> None:
    a, b, c = make(orchard, "A"), make(orchard, "B"), make(orchard, "C")
    orchard.link_tasks(blocker_id=a, blocked_id=b)
    orchard.link_tasks(blocker_id=b, blocked_id=c)
    _close(orchard, b)

    # A blocks B (now closed), which blocks C: C blocking A would close a loop.
    with pytest.raises(CycleError) as caught:
        orchard.link_tasks(blocker_id=c, blocked_id=a)
    statuses = {s["id"]: s["status"] for s in cycle_path(caught)}
    assert statuses[b] == "dropped"

    # unlink_tasks works on the closed task, and then the link is allowed.
    assert orchard.unlink_tasks(blocker_id=a, blocked_id=b)["blocked"]["id"] == b
    orchard.link_tasks(blocker_id=c, blocked_id=a)


def _close(orchard: Orchard, task_id: int) -> None:
    """Close a task without the Phase 3 tools, which do not exist yet."""
    with orchard.tx() as tx:
        task = tx.session.get(Task, task_id)
        task.status = Status.DROPPED
        task.closed_at = tx.now


def test_removing_a_link_or_moving_changes_blocking_on_the_next_read(orchard: Orchard) -> None:
    blocker, project = make(orchard, "Blocker"), make(orchard, "Project")
    step = make(orchard, "Step", parent_id=project)
    orchard.link_tasks(blocker_id=blocker, blocked_id=project)
    assert "blocked" in orchard.get_task(step)["task"]["state"]
    orchard.update_task(step, clear=["parent_id"])
    assert "blocked" not in orchard.get_task(step)["task"].get("state", [])
    orchard.update_task(step, parent_id=project)
    assert "blocked" in orchard.get_task(step)["task"]["state"]
    orchard.unlink_tasks(blocker_id=blocker, blocked_id=project)
    assert "blocked" not in orchard.get_task(step)["task"].get("state", [])
