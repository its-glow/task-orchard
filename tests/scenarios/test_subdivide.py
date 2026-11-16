"""Scenario: breaking down a vague task (the `subdivide` guide, SPEC 9 and 10.2)."""

from __future__ import annotations

from task_orchard.agent.model import STRATEGIC
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from tests.scenarios.harness import run


def test_taxes_are_broken_into_one_layer(orchard: Orchard, clock: FixedClock, seeded) -> None:
    result = run(
        orchard,
        clock,
        STRATEGIC,
        "Filing my taxes feels like a huge blob. Break it down into steps for me. "
        "Go ahead and set them up, no need to check with me first.",
    )

    calls = result.called("subdivide_task")
    assert len(calls) == 1, result.tool_calls
    assert int(calls[0]["parent_id"]) == seeded["taxes"]
    assert 2 <= len(calls[0]["children"]) <= 7
    assert "create_task" not in result.writes
    taxes = orchard.get_task(seeded["taxes"])["task"]
    assert 2 <= taxes["children_open"] <= 7
    assert taxes["achievable"] == "no"
    # One layer only: none of the new children has children of its own.
    for child in taxes["children"]:
        assert orchard.get_task(child["id"])["task"].get("children_open", 0) == 0
