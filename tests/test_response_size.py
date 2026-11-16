"""SPEC 4 and 11 (Phase 5): with 500 tasks, every read tool's response stays
under 20,000 characters.

Every text a user or agent can make long is at its limit: titles, notes,
descriptions, pattern names and learned notes. The structure is wide: a parent
with 300 children, chains deeper than the path shows, a full inbox, many
deadlines inside the critical horizon, many links, conflicts, completions and
patterns, and a full profile. Each task has two conditions and every third a
pattern, as real tasks do.

`worst` puts every row at its maximum (five conditions, a pattern on every
task). A full list of such rows can pass 20,000 characters; that case is held
under WORST_LIMIT so it cannot grow unnoticed (SPEC 13, "Worst-case rows").
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from task_orchard.core.enums import (
    Achievable,
    EventType,
    FieldSource,
    Firmness,
    Level,
    Motive,
    Priority,
    Status,
)
from task_orchard.core.events import record
from task_orchard.core.service import PATTERN_NAME_MAX_CHARS, Orchard
from task_orchard.core.views import PATTERN_DESCRIPTION_MAX_CHARS
from task_orchard.core.wire import CONDITIONS_MAX, TITLE_MAX_CHARS
from task_orchard.db.models import LEARNED_MAX_CHARS, Completion, Pattern, Task, TaskBlock
from task_orchard.tools import Tool, call_tool, catalogue

LIMIT = 20_000
WORST_LIMIT = 30_000
LONG = "w" * (TITLE_MAX_CHARS - 20)


def title(i: int) -> str:
    return f"{i:04d} report {LONG}"


@pytest.fixture
def crowded(orchard: Orchard) -> dict[str, int]:
    return build(orchard, worst=False)


@pytest.fixture
def worst(orchard: Orchard) -> dict[str, int]:
    return build(orchard, worst=True)


def build(orchard: Orchard, *, worst: bool) -> dict[str, int]:
    with orchard.tx() as tx:
        s, now = tx.session, tx.now
        patterns = [
            Pattern(
                name=f"Pattern {i} " + "p" * (PATTERN_NAME_MAX_CHARS - 12),
                description="d" * PATTERN_DESCRIPTION_MAX_CHARS,
                learned="l" * LEARNED_MAX_CHARS,
                created_at=now,
                updated_at=now,
            )
            for i in range(30)
        ]
        s.add_all(patterns)
        s.flush()

        def task(i: int, **kw: Any) -> Task:
            fields = {
                "title": title(i),
                "description": "d" * 5_000,
                "notes": "n" * 20_000,
                "created_at": now - timedelta(days=60),
                "updated_at": now,
                "triaged": True,
                "priority": Priority.HIGH,
                "motive": Motive.WANT if i % 3 == 0 else Motive.MUST,
                "estimate_minutes": 15,
                "activation": Level.LOW,
                "drain": Level.LOW,
                "achievable": Achievable.YES,
                "conditions": (
                    [f"condition-{j}-" + "c" * 18 for j in range(CONDITIONS_MAX)]
                    if worst
                    else ["at-home", "business-hours"]
                ),
                "pattern_id": patterns[i % 30].id if worst or i % 3 == 0 else None,
                "field_sources": {"priority": FieldSource.USER.value},
                **kw,
            }
            row = Task(**fields)
            s.add(row)
            return row

        big = task(0, achievable=Achievable.NO)
        s.flush()
        children = [
            task(
                i,
                parent_id=big.id,
                deadline=now + timedelta(hours=i % 20 - 5),
                deadline_firmness=Firmness.HARD,
            )
            for i in range(1, 301)
        ]
        chain_top = task(301, achievable=Achievable.NO)
        s.flush()
        below = chain_top
        for i in range(302, 322):  # 20 deep
            below = task(i, parent_id=below.id, achievable=Achievable.NO)
            s.flush()
        inbox = [task(i, triaged=False) for i in range(322, 400)]
        done = [
            task(i, status=Status.DONE, closed_at=now - timedelta(days=1)) for i in range(400, 500)
        ]
        s.flush()
        for i, blocked in enumerate(children[:100]):
            s.add(TaskBlock(blocker_id=children[-1 - i].id, blocked_id=blocked.id, created_at=now))
        for row in done:
            s.add(
                Completion(
                    task_id=row.id,
                    completed_at=now - timedelta(days=1),
                    actual_minutes=30,
                    estimate_at_completion=15,
                    estimate_source_at_completion=FieldSource.GUESS,
                    felt_activation=Level.HIGH,
                    reflection="r" * 2_000,
                    motive_at_completion=Motive.MUST,
                )
            )
        for row in children[:100] + inbox:
            for kind in (EventType.SNOOZED, EventType.STARTED, EventType.STARTED):
                record(s, kind, at=now - timedelta(days=2), task_id=row.id, payload={})
            record(
                s,
                EventType.CONFLICT,
                at=now - timedelta(days=1),
                task_id=row.id,
                payload={
                    "field": "priority",
                    "kept": "high",
                    "proposed": "low",
                    "source": "pattern",
                },
            )
        return {"big": big.id, "deep": below.id, "chain": chain_top.id, "inbox": inbox[0].id}


@pytest.fixture
def full_profile(orchard: Orchard) -> None:
    orchard.write_profile_note("charter", "c" * 4_000)
    for i in range(8):
        orchard.write_profile_note(f"topic {i} " + "t" * 80, "n" * 990)


CALLS: list[tuple[str, Any]] = [
    ("get_task", lambda ids: {"task_id": ids["big"]}),
    ("get_task", lambda ids: {"task_id": ids["deep"]}),
    ("get_task", lambda ids: {"task_id": ids["inbox"]}),
    ("get_tree", lambda ids: {"depth": 5}),
    ("get_tree", lambda ids: {"root_id": ids["big"], "depth": 5, "include_closed": True}),
    ("get_tree", lambda ids: {"root_id": ids["chain"], "depth": 5}),
    ("search_tasks", lambda ids: {"query": "report", "limit": 30, "status": "any"}),
    ("search_tasks", lambda ids: {"inbox_only": True, "limit": 30}),
    ("get_candidates", lambda ids: {"limit": 30, "reserve_wants": 10}),
    ("get_candidates", lambda ids: {"limit": 30, "under_id": ids["big"]}),
    ("get_critical", lambda ids: {"horizon_days": 30}),
    ("get_review_queue", lambda ids: {}),
    ("get_recent_activity", lambda ids: {"days": 31}),
    ("find_patterns", lambda ids: {"limit": 10}),
    ("find_patterns", lambda ids: {"query": "pattern", "limit": 10}),
    ("read_profile", lambda ids: {}),
]


@pytest.mark.parametrize(
    ("name", "arguments"), CALLS, ids=[f"{n}-{i}" for i, (n, _) in enumerate(CALLS)]
)
def test_every_read_stays_under_the_limit(
    orchard: Orchard, crowded: dict[str, int], full_profile: None, name: str, arguments: Any
) -> None:
    tools: list[Tool] = catalogue(orchard)
    outcome = call_tool(tools, name, arguments(crowded))
    assert not outcome.is_error, outcome.payload
    assert len(json.dumps(outcome.payload)) > 500  # the call found something to show
    size = len(json.dumps(outcome.payload, ensure_ascii=False))
    assert size < LIMIT, (name, size)


def test_every_read_tool_is_covered(orchard: Orchard) -> None:
    reads = {t.name for t in catalogue(orchard) if t.read_only}
    assert reads == {name for name, _ in CALLS}


LISTS = [call for call in CALLS if call[0] in {"search_tasks", "get_candidates", "get_critical"}]


@pytest.mark.parametrize(("name", "arguments"), LISTS, ids=[n for n, _ in LISTS])
def test_lists_of_worst_case_rows_stay_bounded(
    orchard: Orchard, worst: dict[str, int], name: str, arguments: Any
) -> None:
    outcome = call_tool(catalogue(orchard), name, arguments(worst))
    assert not outcome.is_error, outcome.payload
    assert len(json.dumps(outcome.payload, ensure_ascii=False)) < WORST_LIMIT
