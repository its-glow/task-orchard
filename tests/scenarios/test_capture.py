"""Scenario: capturing three things at once (README shakedown, SPEC 10.2)."""

from __future__ import annotations

from task_orchard.agent.model import BASIC
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from tests.scenarios.harness import run


def test_three_things_in_one_capture(orchard: Orchard, clock: FixedClock, seeded) -> None:
    inbox_before = orchard.search_tasks(inbox_only=True)["total"]

    result = run(orchard, clock, BASIC, "Remind me to buy stamps, call Mum and fix the bike light.")

    captures = result.called("capture_tasks")
    assert len(captures) == 1, result.tool_calls
    assert len(captures[0]["items"]) == 3
    assert orchard.search_tasks(inbox_only=True)["total"] == inbox_before + 3
    assert result.writes == ["capture_tasks"]
