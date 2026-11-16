"""SPEC 5.7 and 8.13: the shortlist."""

from __future__ import annotations

from typing import Any

import pytest

from task_orchard.core.clock import FixedClock
from task_orchard.core.errors import Invalid, NotFound
from task_orchard.core.service import Orchard


def make(orchard: Orchard, title: str, **kw: Any) -> int:
    return orchard.create_task(title=title, **kw)["task"]["id"]


def ids(result: dict[str, Any]) -> list[int]:
    return [row["id"] for row in result["candidates"]]


def pool_adds_up(result: dict[str, Any]) -> None:
    pool = result["pool"]
    assert set(pool["cut"]) == {"pressing", "due_soon", "other"}
    assert pool["total"] == pool["shown"] + sum(pool["cut"].values())
    assert pool["shown"] == len(result["candidates"])


# --- Against the seed ---


def test_seeded_shortlist(orchard: Orchard, seeded: dict[str, int]) -> None:
    result = orchard.get_candidates()
    rows = {row["id"]: row for row in result["candidates"]}
    assert rows[seeded["release_flaky"]]["group"] == "pressing"
    assert rows[seeded["cert"]]["group"] == "pressing"
    instance = rows[seeded["summary_w3"]]
    assert "overdue" in instance["state"]
    assert instance["instance_of"] == seeded["summary"]
    assert seeded["laundry"] in rows  # an awake flexible task
    for absent in ("taxes", "release_notes", "offsite_venue", "inbox_desk", "summary", "run"):
        assert seeded[absent] not in rows, absent
    assert result["inbox"] == 3
    assert result["conditions_in_pool"] == ["at-home", "business-hours", "uninterrupted"]
    assert "filtered" not in result
    pool_adds_up(result)


def test_a_want_is_kept_in_view_even_in_a_short_list(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    result = orchard.get_candidates(limit=5)
    wants = {seeded["novel"], seeded["game_movement"], seeded["game_build"]}
    reserved = [row for row in result["candidates"] if row.get("reserved")]
    assert len(reserved) == 1 and reserved[0]["id"] in wants
    assert reserved[0]["motive"] == "want"
    assert seeded["obs_spec"] not in ids(result)  # the "other" row it replaced
    pool_adds_up(result)


# --- Grouping and order ---


def test_groups_and_order(orchard: Orchard) -> None:
    pressing = make(orchard, "Hard tomorrow", deadline="+20h", firmness="hard")
    soft_overdue = make(orchard, "Soft overdue", deadline="2026-03-10")
    soon = make(orchard, "Due in two days", deadline="+2d")
    later = make(orchard, "Due next month", deadline="+30d", priority="high")
    high = make(orchard, "High, no date", priority="high")
    plain = make(orchard, "Plain")
    low = make(orchard, "Low", priority="low")
    result = orchard.get_candidates()
    groups = {row["id"]: row["group"] for row in result["candidates"]}
    assert groups[pressing] == "pressing"
    assert groups[soft_overdue] == groups[soon] == "due_soon"
    assert groups[later] == groups[high] == groups[plain] == groups[low] == "other"
    # Within "other": priority (unset counts as normal), then due, then age.
    assert ids(result) == [pressing, soft_overdue, soon, later, high, plain, low]


def test_soft_deadlines_are_not_pressing_but_a_hard_one_behind_them_is(orchard: Orchard) -> None:
    project = make(orchard, "Project", deadline="+20h", firmness="hard")
    step = make(orchard, "Step", parent_id=project, deadline="+10h")
    [row] = orchard.get_candidates(under_id=step)["candidates"]
    assert row["group"] == "pressing"
    assert row["due"]["firmness"] == "soft" and row["hard_due"]["firmness"] == "hard"


# --- Filters ---


@pytest.fixture
def mixed(orchard: Orchard) -> dict[str, int]:
    project = make(orchard, "Project")
    return {
        "project": project,
        "inside": make(orchard, "Inside", parent_id=project, estimate_minutes=20),
        "long": make(orchard, "Long", estimate_minutes=240, priority="high"),
        "outdoor": make(orchard, "Outdoor", conditions=["good-weather"], deadline="+2d"),
        "low": make(orchard, "Low", priority="low", deadline="+1d"),
        "tiring": make(orchard, "Tiring", drain="high"),
    }


def test_under_id(orchard: Orchard, mixed: dict[str, int]) -> None:
    result = orchard.get_candidates(under_id=mixed["inside"])
    assert ids(result) == [mixed["inside"]]  # the task itself
    result = orchard.get_candidates(under_id=mixed["project"])
    assert ids(result) == [mixed["inside"]]  # the project has open children
    assert result["filtered"] == {"outside": 4}
    with pytest.raises(NotFound):
        orchard.get_candidates(under_id=999)


def test_min_priority(orchard: Orchard, mixed: dict[str, int]) -> None:
    result = orchard.get_candidates(min_priority="normal")
    assert mixed["low"] not in ids(result) and mixed["tiring"] in ids(result)
    assert result["filtered"] == {"below_priority": 1}
    assert ids(orchard.get_candidates(min_priority="HIGH")) == [mixed["long"]]


def test_due_within_days(orchard: Orchard, mixed: dict[str, int]) -> None:
    # "+1d" is 23:59 tomorrow (38 hours away); "+2d" is 23:59 the day after (62 hours).
    result = orchard.get_candidates(due_within_days=2)
    assert ids(result) == [mixed["low"]]
    assert result["filtered"] == {"not_due": 4}
    assert set(ids(orchard.get_candidates(due_within_days=2.6))) == {mixed["low"], mixed["outdoor"]}


def test_due_within_days_includes_its_end(orchard: Orchard) -> None:
    edge = make(orchard, "Edge", deadline="+48h")
    assert ids(orchard.get_candidates(due_within_days=2)) == [edge]
    assert ids(orchard.get_candidates(due_within_days=1.99)) == []


def test_minutes_available(orchard: Orchard, mixed: dict[str, int]) -> None:
    result = orchard.get_candidates(minutes_available=60)
    assert mixed["long"] not in ids(result)
    assert mixed["tiring"] in ids(result)  # no estimate: stays
    assert result["filtered"] == {"too_long": 1}


def test_exclude_conditions(orchard: Orchard, mixed: dict[str, int]) -> None:
    result = orchard.get_candidates(exclude_conditions=["Good Weather"])
    assert mixed["outdoor"] not in ids(result)
    assert result["filtered"] == {"conditions": 1}
    assert result["conditions_in_pool"] == ["good-weather"]  # counted before filtering


def test_filters_combine_and_the_first_one_counts(orchard: Orchard, mixed: dict[str, int]) -> None:
    result = orchard.get_candidates(
        min_priority="normal", minutes_available=60, exclude_conditions=["good-weather"]
    )
    # `low` fails priority only; `long` fails time; `outdoor` fails conditions.
    assert result["filtered"] == {"below_priority": 1, "too_long": 1, "conditions": 1}
    assert set(ids(result)) == {mixed["inside"], mixed["tiring"]}
    # A task removed by two filters counts under the first.
    result = orchard.get_candidates(min_priority="high", minutes_available=10)
    assert result["filtered"] == {"below_priority": 4, "too_long": 1}
    pool_adds_up(result)


def test_energy_marks_but_never_filters(orchard: Orchard, mixed: dict[str, int]) -> None:
    result = orchard.get_candidates(energy="low")
    rows = {row["id"]: row for row in result["candidates"]}
    assert len(rows) == 5
    assert rows[mixed["tiring"]]["over_energy"] is True
    assert "over_energy" not in rows[mixed["inside"]]  # no drain set
    assert "filtered" not in result


@pytest.mark.parametrize(
    "kw",
    [
        {"limit": 0},
        {"limit": 31},
        {"reserve_wants": 11},
        {"due_within_days": -1},
        {"due_within_days": 366},
        {"minutes_available": 0},
        {"energy": "extreme"},
        {"min_priority": "urgent"},
    ],
)
def test_parameters_are_checked(orchard: Orchard, kw: dict) -> None:
    with pytest.raises(Invalid):
        orchard.get_candidates(**kw)


# --- Reserved wants ---


def test_reserved_wants_replace_only_other_non_wants(orchard: Orchard) -> None:
    for n in range(3):
        make(orchard, f"Pressing {n}", deadline="+10h", firmness="hard")
    others = [make(orchard, f"Chore {n}", priority="high") for n in range(4)]
    wants = [make(orchard, f"Want {n}", motive="want", priority="low") for n in range(4)]

    result = orchard.get_candidates(limit=6)  # reserve 2
    assert [row["group"] for row in result["candidates"]][:3] == ["pressing"] * 3
    reserved = [row["id"] for row in result["candidates"] if row.get("reserved")]
    assert reserved == wants[:2]
    assert others[0] in ids(result)  # the highest-ordered chore keeps its place
    assert others[1] not in ids(result)
    pool_adds_up(result)

    # The pressing rows are never replaced, so with only pressing rows nothing changes.
    result = orchard.get_candidates(limit=3)
    assert not any(row.get("reserved") for row in result["candidates"])


def test_the_reserve_is_a_third_of_the_limit(orchard: Orchard) -> None:
    top = make(orchard, "Top", priority="critical")
    for n in range(3):
        make(orchard, f"Chore {n}", priority="high")
    want = make(orchard, "Want", motive="want", priority="low")
    assert ids(orchard.get_candidates(limit=1)) == [top]
    assert want not in ids(orchard.get_candidates(limit=2))
    assert want in ids(orchard.get_candidates(limit=3))
    assert want not in ids(orchard.get_candidates(limit=3, reserve_wants=0))


def test_a_want_already_shown_counts_toward_the_reserve(orchard: Orchard) -> None:
    want = make(orchard, "Want", motive="want", priority="critical")
    for n in range(5):
        make(orchard, f"Chore {n}")
    make(orchard, "Second want", motive="want", priority="low")
    result = orchard.get_candidates(limit=3, reserve_wants=1)
    assert ids(result)[0] == want
    assert not any(row.get("reserved") for row in result["candidates"])


def test_dormant_tasks_return_to_the_pool(orchard: Orchard, clock: FixedClock) -> None:
    task = make(orchard, "Later", snooze_until="+2d")
    assert task not in ids(orchard.get_candidates())
    clock.advance(days=2)
    assert task in ids(orchard.get_candidates())
