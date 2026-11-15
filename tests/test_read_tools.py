"""SPEC 4.3, 4.4 and 8.12: rows, details, get_tree and search_tasks."""

from __future__ import annotations

from typing import Any

import pytest

from task_orchard.core.errors import Invalid, NotFound
from task_orchard.core.service import Orchard
from task_orchard.db.models import Task

ELLIPSIS = "…"


def make(orchard: Orchard, title: str, **kw: Any) -> int:
    return orchard.create_task(title=title, **kw)["task"]["id"]


# --- Rows and details ---


def test_every_response_carries_now(orchard: Orchard) -> None:
    task = make(orchard, "T")
    assert orchard.get_task(task)["now"] == {"at": "2026-03-18T10:00", "day": "Wed"}
    assert orchard.search_tasks()["now"]["day"] == "Wed"


def test_a_bare_task_has_only_the_keys_that_say_something(orchard: Orchard) -> None:
    task = make(orchard, "Plain")
    detail = orchard.get_task(task)["task"]
    assert set(detail) == {"id", "title", "achievable", "recent_events", "created"}
    assert detail["recent_events"] == [{"type": "created", "when": "now", "note": "via create"}]
    row = orchard.search_tasks()["results"][0]
    assert row == {"id": task, "title": "Plain", "status": "open", "achievable": "unknown"}


def test_detail_shape(orchard: Orchard, seeded: dict[str, int]) -> None:
    detail = orchard.get_task(seeded["observability"])["task"]
    assert detail["achievable"] == "no"
    assert detail["children_open"] == 2 and detail["children_closed"] == 1
    assert {c["id"] for c in detail["children"]} == {seeded["obs_spec"], seeded["obs_collector"]}
    assert detail["due"]["firmness"] == "soft"
    assert detail["due"]["rel"] == "in 21 days"
    assert detail["created"]["rel"].endswith("ago")

    done = orchard.get_task(seeded["obs_backend"])["task"]
    assert done["status"] == "done"
    assert done["completion"]["actual_minutes"] == 150
    assert "closed" in done
    assert "state" not in done

    blocker = orchard.get_task(seeded["cert"])["task"]
    assert blocker["blocks"] == [
        {"id": seeded["release_publish"], "title": "Publish the signed build", "status": "open"}
    ]
    assert blocker["unblocks"] == 1
    assert blocker["priority"] == "low"  # own priority until Phase 2

    snoozed = orchard.get_task(seeded["taxes"])["task"]
    assert snoozed["snoozed_until"]["rel"] == "in 20 days"


def test_recurring_rows(orchard: Orchard, seeded: dict[str, int]) -> None:
    laundry = orchard.get_task(seeded["laundry"])["task"]
    assert laundry["recurring"]["mode"] == "flexible"
    assert laundry["recurring"]["gap_days"] == 5
    assert "achievable" not in laundry
    assert "achievable" not in laundry.get("fields", {})
    instance = orchard.get_task(seeded["summary_w3"])["task"]
    assert instance["instance_of"] == seeded["summary"]
    assert "overdue" in instance["state"]
    assert instance["due"]["rel"].startswith("overdue by")


def test_titles_are_truncated(orchard: Orchard) -> None:
    long = "A" * 100
    parent = make(orchard, long)
    child = make(orchard, "B" * 100, parent_id=parent)
    row = orchard.search_tasks(query="B" * 100)["results"][0]
    assert row["title"] == "B" * 79 + ELLIPSIS
    assert len(row["title"]) == 80
    assert row["path"] == ["A" * 39 + ELLIPSIS]
    detail = orchard.get_task(child)["task"]
    assert detail["parent"]["title"] == "A" * 39 + ELLIPSIS


def test_path_shows_the_nearest_three_ancestors(orchard: Orchard) -> None:
    ids = [make(orchard, "L0")]
    for n in range(1, 6):
        ids.append(make(orchard, f"L{n}", parent_id=ids[-1]))
    assert orchard.get_task(ids[2])["task"]["path"] == ["L0", "L1"]
    assert orchard.get_task(ids[3])["task"]["path"] == ["L0", "L1", "L2"]
    assert orchard.get_task(ids[5])["task"]["path"] == [ELLIPSIS, "L2", "L3", "L4"]


def test_row_lists_at_most_three_blockers(orchard: Orchard) -> None:
    blockers = [make(orchard, f"Blocker {n}") for n in range(5)]
    task = make(orchard, "Waiting", blocked_by=blockers)
    row = orchard.search_tasks(query="waiting")["results"][0]
    assert [b["id"] for b in row["blocked_by"]] == blockers[:3]
    assert row["blocked_by_more"] == 2
    assert len(orchard.get_task(task)["task"]["blocked_by"]) == 5


def test_notes_are_cut_in_the_detail(orchard: Orchard) -> None:
    task = make(orchard, "T", notes="n" * 2500)
    detail = orchard.get_task(task)["task"]
    assert len(detail["notes"]) == 2000
    assert detail["notes_truncated"] is True


def test_get_task_not_found(orchard: Orchard) -> None:
    with pytest.raises(NotFound) as caught:
        orchard.get_task(12345)
    assert "search_tasks" in caught.value.hint


# --- get_tree ---


def test_tree_of_the_seed(orchard: Orchard, seeded: dict[str, int]) -> None:
    result = orchard.get_tree()
    roots = {node["id"]: node for node in result["tree"]}
    assert seeded["observability"] in roots
    assert seeded["obs_spec"] not in roots
    obs = roots[seeded["observability"]]
    assert [c["id"] for c in obs["children"]] == [seeded["obs_spec"], seeded["obs_collector"]]
    assert "status" not in obs
    assert roots[seeded["summary"]]["recurring"] == "scheduled"
    assert "truncated" not in result


def test_tree_depth_and_more(orchard: Orchard) -> None:
    top = make(orchard, "Top")
    mid = make(orchard, "Mid", parent_id=top)
    low = make(orchard, "Low", parent_id=mid)
    make(orchard, "Lowest", parent_id=low)
    [node] = orchard.get_tree(root_id=top, depth=1)["tree"]
    assert node == {"id": top, "title": "Top", "state": ["has_open_children"], "more": 3}
    [node] = orchard.get_tree(root_id=top, depth=2)["tree"]
    assert node["children"][0]["more"] == 2
    with pytest.raises(Invalid):
        orchard.get_tree(depth=6)


def test_tree_closed_tasks_and_order(orchard: Orchard, seeded: dict[str, int]) -> None:
    obs = seeded["observability"]
    [node] = orchard.get_tree(root_id=obs)["tree"]
    assert seeded["obs_backend"] not in [c["id"] for c in node["children"]]
    [node] = orchard.get_tree(root_id=obs, include_closed=True)["tree"]
    kids = node["children"]
    assert kids[-1] == {"id": seeded["obs_backend"], "title": kids[-1]["title"], "status": "done"}


def test_tree_is_cut_at_100_nodes(orchard: Orchard) -> None:
    top = make(orchard, "Top")
    with orchard.tx() as tx:  # direct rows: 120 service calls would be slow
        for n in range(120):
            tx.session.add(
                Task(title=f"Child {n}", parent_id=top, created_at=tx.now, updated_at=tx.now)
            )
    result = orchard.get_tree(root_id=top)
    assert result["truncated"] is True
    [node] = result["tree"]
    assert len(node["children"]) == 99
    assert node["more"] == 21


# --- search_tasks ---


def test_search_finds_words_with_stemming(orchard: Orchard) -> None:
    spec = make(orchard, "Write the interface specs")
    make(orchard, "Something else", notes="mentions the spec in its notes")
    result = orchard.search_tasks(query="spec")
    assert result["total"] == 2
    assert spec in {r["id"] for r in result["results"]}
    assert all(r["status"] == "open" for r in result["results"])


def test_search_requires_all_words_then_any(orchard: Orchard) -> None:
    both = make(orchard, "Call the dentist")
    one = make(orchard, "Call the mechanic")
    assert [r["id"] for r in orchard.search_tasks(query="call dentist")["results"]] == [both]
    ids = {r["id"] for r in orchard.search_tasks(query="dentist mechanic")["results"]}
    assert ids == {both, one}


@pytest.mark.parametrize(
    "query",
    ['"dentist', 'dentist"', "dentist*", "NEAR(dentist call)", "dentist AND", "-dentist", "a:b"],
)
def test_hostile_queries_are_quoted(orchard: Orchard, query: str) -> None:
    make(orchard, "Call the dentist")
    make(orchard, "Near the station")
    orchard.search_tasks(query=query)  # never an FTS syntax error


@pytest.mark.parametrize("query", ["*", "--", '""', "()", "_", "__"])
def test_a_query_with_no_words_is_invalid(orchard: Orchard, query: str) -> None:
    with pytest.raises(Invalid) as caught:
        orchard.search_tasks(query=query)
    assert "omit" in caught.value.hint or "leave query out" in caught.value.hint


def test_search_status_inbox_and_limit(orchard: Orchard, seeded: dict[str, int]) -> None:
    closed = orchard.search_tasks(status="closed")["results"]
    assert closed and all(r["status"] != "open" for r in closed)
    inbox = orchard.search_tasks(inbox_only=True)
    assert {r["id"] for r in inbox["results"]} == {
        seeded["inbox_desk"],
        seeded["inbox_reply"],
        seeded["inbox_run"],
    }
    assert len(orchard.search_tasks(status="any", limit=5)["results"]) == 5
    with pytest.raises(Invalid):
        orchard.search_tasks(limit=31)
    with pytest.raises(Invalid):
        orchard.search_tasks(status="finished")


def test_search_without_a_query_lists_recent_first(orchard: Orchard, clock: Any) -> None:
    first = make(orchard, "First")
    clock.advance(minutes=5)
    second = make(orchard, "Second")
    clock.advance(minutes=5)
    orchard.update_task(first, priority="low")
    assert [r["id"] for r in orchard.search_tasks()["results"]] == [first, second]


def test_an_empty_search_keeps_results_and_total(orchard: Orchard) -> None:
    result = orchard.search_tasks(query="nothing")
    assert result["results"] == [] and result["total"] == 0
