"""SPEC 8.4: subdivide_task."""

from __future__ import annotations

from typing import Any

import pytest

from task_orchard.core.errors import CycleError, Invalid, NotFound, ParentClosed, Unsupported
from task_orchard.core.service import Orchard
from tests.helpers import counts, events, last_event_id, make


def subdivide(orchard: Orchard, parent: int, *titles: str, **kw: Any) -> dict[str, Any]:
    return orchard.subdivide_task(parent, [{"title": t} for t in titles], **kw)


def blockers(orchard: Orchard, task_id: int) -> list[int]:
    detail = orchard.get_task(task_id)["task"]
    return [b["id"] for b in detail.get("blocked_by", [])]


def test_children_are_created_triaged_under_the_parent(orchard: Orchard) -> None:
    parent = make(orchard, "Plan the offsite", achievable="yes", sources={"achievable": "user"})
    result = orchard.subdivide_task(
        parent,
        [
            {"title": "Pick a date", "estimate_minutes": 15, "achievable": "yes"},
            {"title": "Book a venue", "priority": "high", "conditions": ["Business Hours"]},
        ],
    )
    children = result["children"]
    assert [c["title"] for c in children] == ["Pick a date", "Book a venue"]
    assert all(c["path"] == ["Plan the offsite"] for c in children)
    assert all("inbox" not in c.get("state", []) for c in children)
    assert children[0]["estimate_minutes"] == 15
    assert "achievable" not in children[0]  # yes is omitted
    assert children[1]["achievable"] == "unknown"
    assert children[1]["conditions"] == ["business-hours"]
    # The parent is a container now, even though the user had said yes.
    assert result["parent"]["achievable"] == "no"
    assert "has_open_children" in result["parent"]["state"]
    fields = orchard.get_task(parent)["task"]["fields"]
    assert fields["achievable"] == {"value": "no", "source": "evidence"}


def test_sources_apply_to_every_child(orchard: Orchard) -> None:
    parent = make(orchard)
    result = orchard.subdivide_task(
        parent,
        [{"title": "A", "estimate_minutes": 10}, {"title": "B", "estimate_minutes": 20}],
        sources={"estimate_minutes": "user"},
    )
    for child in result["children"]:
        fields = orchard.get_task(child["id"])["task"]["fields"]
        assert fields["estimate_minutes"]["source"] == "user"


def test_sequential_chains_each_child_to_the_one_before(orchard: Orchard) -> None:
    parent = make(orchard)
    ids = [c["id"] for c in subdivide(orchard, parent, "A", "B", "C", sequential=True)["children"]]
    assert blockers(orchard, ids[0]) == []
    assert blockers(orchard, ids[1]) == [ids[0]]
    assert blockers(orchard, ids[2]) == [ids[1]]


def test_after_names_positions_in_the_same_call(orchard: Orchard) -> None:
    parent = make(orchard)
    result = orchard.subdivide_task(
        parent,
        [
            {"title": "Draft"},
            {"title": "Review"},
            {"title": "Send", "after": [1, 2]},
            {"title": "Review again", "after": 2},
        ],
    )
    ids = [c["id"] for c in result["children"]]
    assert sorted(blockers(orchard, ids[2])) == [ids[0], ids[1]]
    assert blockers(orchard, ids[3]) == [ids[1]]
    assert blockers(orchard, ids[1]) == []


def test_ordering_that_loops_is_rejected_and_creates_nothing(orchard: Orchard) -> None:
    parent = make(orchard)
    before = counts(orchard)
    with pytest.raises(CycleError):
        orchard.subdivide_task(parent, [{"title": "A", "after": [2]}, {"title": "B", "after": [1]}])
    assert counts(orchard) == before


@pytest.mark.parametrize(
    "children",
    [
        [{"title": "Fine"}, {"title": "  "}],
        [{"title": "Fine"}, {"title": "Bad", "priority": "urgent"}],
        [{"title": "Fine"}, {"title": "Bad", "estimate_minutes": 0}],
        [{"title": "Fine"}, {"title": "Bad", "deadline": "friday"}],
        [{"title": "Fine"}, {"title": "Bad", "after": [3]}],
        [{"title": "Fine"}, {"title": "Bad", "after": [2]}],
        [{"title": "Fine"}, "not an object"],
        [],
        [{"title": str(i)} for i in range(13)],
    ],
)
def test_one_bad_child_creates_none(orchard: Orchard, children: list[Any]) -> None:
    parent = make(orchard)
    before = counts(orchard)
    with pytest.raises(Invalid):
        orchard.subdivide_task(parent, children)
    assert counts(orchard) == before


def test_a_bad_child_is_named_in_the_error(orchard: Orchard) -> None:
    parent = make(orchard)
    with pytest.raises(Invalid) as caught:
        orchard.subdivide_task(parent, [{"title": "Fine"}, {"title": "Bad", "drain": "huge"}])
    assert caught.value.message.startswith("Child 2:")


def test_after_and_sequential_together_are_invalid(orchard: Orchard) -> None:
    parent = make(orchard)
    with pytest.raises(Invalid):
        orchard.subdivide_task(
            parent, [{"title": "A"}, {"title": "B", "after": [1]}], sequential=True
        )


def test_parent_must_be_open_plain_and_exist(orchard: Orchard, seeded: dict[str, int]) -> None:
    with pytest.raises(NotFound):
        subdivide(orchard, 9999, "A")
    with pytest.raises(Unsupported):
        subdivide(orchard, seeded["laundry"], "A")
    done = make(orchard)
    orchard.complete_task(done)
    with pytest.raises(ParentClosed):
        subdivide(orchard, done, "A")


def test_a_task_with_children_can_be_subdivided_again(orchard: Orchard) -> None:
    parent = make(orchard)
    subdivide(orchard, parent, "A")
    subdivide(orchard, parent, "B")
    assert orchard.get_task(parent)["task"]["children_open"] == 2


def test_events(orchard: Orchard) -> None:
    parent = make(orchard)
    since = last_event_id(orchard)
    ids = [c["id"] for c in subdivide(orchard, parent, "A", "B", sequential=True)["children"]]
    assert events(orchard, since) == [
        ("created", ids[0], {"via": "subdivide", "parent_id": parent}),
        ("created", ids[1], {"via": "subdivide", "parent_id": parent}),
        ("linked", ids[1], {"blocker_id": ids[0], "blocked_id": ids[1]}),
        ("subdivided", parent, {"children": ids}),
        (
            "updated",
            parent,
            {"changes": {"achievable": {"from": "unknown", "to": "no", "source": "evidence"}}},
        ),
    ]
