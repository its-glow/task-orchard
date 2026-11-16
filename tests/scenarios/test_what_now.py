"""Scenario: "I've got 30 minutes and I'm tired" (README shakedown, SPEC 10.2)."""

from __future__ import annotations

from task_orchard.agent.model import BASIC
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from tests.scenarios.harness import run


def test_tired_with_30_minutes(orchard: Orchard, clock: FixedClock, seeded) -> None:
    result = run(orchard, clock, BASIC, "I've got 30 minutes and I'm tired. What should I do?")

    shortlists = result.called("get_candidates")
    assert shortlists, result.tool_calls
    assert any(
        str(args.get("minutes_available")) == "30"
        and str(args.get("energy", "")).strip().lower() == "low"
        for args in shortlists
    ), shortlists
    assert result.writes == []
