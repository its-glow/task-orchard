"""SPEC 5.8 and 8.14: the critical list."""

from __future__ import annotations

from typing import Any

import pytest

from task_orchard.core.enums import Priority
from task_orchard.core.errors import Invalid
from task_orchard.core.service import Orchard
from task_orchard.db.models import Task


def make(orchard: Orchard, title: str, **kw: Any) -> int:
    return orchard.create_task(title=title, **kw)["task"]["id"]


def listed(orchard: Orchard, **kw: Any) -> dict[int, dict[str, Any]]:
    return {row["id"]: row for row in orchard.get_critical(**kw)["critical"]}


def test_blocked_dormant_and_inbox_leaves_are_included(orchard: Orchard) -> None:
    blocker = make(orchard, "Blocker")
    project = make(orchard, "Project", blocked_by=[blocker])
    blocked = make(orchard, "Blocked step", parent_id=project, priority="critical")
    dormant = make(orchard, "Dormant", priority="critical", snooze_until="+5d")
    [captured] = orchard.capture_tasks([{"title": "Captured"}])["captured"]
    orchard.update_task(captured["id"], deadline="+12h", triaged=False)

    rows = listed(orchard)
    # The blocker is listed too: it inherits `critical` from the step it holds up.
    assert set(rows) == {blocker, blocked, dormant, captured["id"]}
    assert rows[blocker]["priority_via"]["id"] == project
    assert rows[blocked]["blocked_by"] == [
        {"id": blocker, "title": "Blocker", "via": {"id": project, "title": "Project"}}
    ]
    assert "dormant" in rows[dormant]["state"]
    assert "inbox" in rows[captured["id"]]["state"]
    assert orchard.get_critical()["inbox"] == 1


def test_up_to_five_blockers_are_shown(orchard: Orchard) -> None:
    blockers = [make(orchard, f"Blocker {n}") for n in range(7)]
    task = make(orchard, "Waiting", priority="critical", blocked_by=blockers)
    row = listed(orchard)[task]
    assert [b["id"] for b in row["blocked_by"]] == blockers[:5]
    assert row["blocked_by_more"] == 2
    assert all("via" not in b for b in row["blocked_by"])  # its own blockers


def test_parents_and_recurring_tasks_are_not_listed(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    rows = listed(orchard)
    assert seeded["release"] not in rows  # a parent
    assert seeded["summary"] not in rows  # recurring
    assert seeded["summary_w3"] in rows  # its overdue instance is a leaf
    assert {seeded["release_flaky"], seeded["release_notes"], seeded["cert"]} <= set(rows)
    assert seeded["milk"] in rows  # overdue, if only softly
    assert seeded["obs_spec"] not in rows


def test_horizon_boundaries(orchard: Orchard) -> None:
    # Beyond 24 hours, so not at risk: only the horizon can list them.
    edge = make(orchard, "Due in exactly two days", deadline="+48h")
    beyond = make(orchard, "Due a minute later", deadline="+2881m")
    assert listed(orchard) == {}
    assert edge in listed(orchard, horizon_days=2)  # the end is included
    assert beyond not in listed(orchard, horizon_days=2)
    assert edge not in listed(orchard, horizon_days=1.99)
    # Anything due within 24 hours is at risk, so a horizon of 0 still lists it.
    soon = make(orchard, "Due tonight", deadline="+8h")
    assert list(listed(orchard, horizon_days=0)) == [soon]
    for bad in (-1, 31):
        with pytest.raises(Invalid):
            orchard.get_critical(horizon_days=bad)


def test_at_risk_is_judged_on_the_work_of_whoever_owns_the_deadline(orchard: Orchard) -> None:
    # Due in three days (86 hours) with thirty hours of open work: 3 x 30 > 86.
    project = make(orchard, "Big project", deadline="+3d")
    shared = [make(orchard, f"Part {n}", parent_id=project, estimate_minutes=580) for n in range(3)]
    # Its own, earlier deadline: judged on its own hour, so not at risk.
    own = make(orchard, "Own deadline", parent_id=project, deadline="+48h", estimate_minutes=60)

    rows = listed(orchard)
    for part in shared:
        assert "at_risk" in rows[part]["state"]
    assert own not in rows
    assert own in listed(orchard, horizon_days=2)
    assert "at_risk" not in listed(orchard, horizon_days=2)[own].get("state", [])

    # With less work the project's leaves are no longer at risk.
    orchard.update_task(shared[0], estimate_minutes=30)
    orchard.update_task(shared[1], estimate_minutes=30)
    assert listed(orchard) == {}


def test_order_and_cap(orchard: Orchard) -> None:
    late = make(orchard, "Later", deadline="+20h")
    early = make(orchard, "Sooner", deadline="+2h")
    undated = make(orchard, "Critical, no date", priority="critical")
    assert list(listed(orchard)) == [early, late, undated]

    with orchard.tx() as tx:  # direct rows: 30 service calls would be slow
        for n in range(30):
            tx.session.add(
                Task(
                    title=f"Critical {n}",
                    priority=Priority.CRITICAL,
                    triaged=True,
                    created_at=tx.now,
                    updated_at=tx.now,
                )
            )
    result = orchard.get_critical()
    assert len(result["critical"]) == 30
    assert result["omitted"] == 3
