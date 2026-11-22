"""Scenario: "I called the dentist" (the `complete-and-reflect` guide, SPEC 9 and 10.2)."""

from __future__ import annotations

from task_orchard.agent.model import BASIC
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from tests.scenarios.harness import run


def test_a_completion_is_recorded_with_how_it_went(
    orchard: Orchard, clock: FixedClock, seeded
) -> None:
    result = run(
        orchard,
        clock,
        BASIC,
        "I finally called the dentist and booked the check-up. Only took about 10 minutes, "
        "but I dreaded it all morning.",
    )

    calls = result.called("complete_task")
    assert len(calls) == 1, result.tool_calls
    args = calls[0]
    assert int(args["task_id"]) == seeded["dentist"]
    assert int(args["actual_minutes"]) == 10
    assert str(args.get("felt_activation", "")).lower() == "high"
    assert set(result.writes) <= {"complete_task", "save_pattern"}
    dentist = orchard.get_task(seeded["dentist"])["task"]
    assert dentist["status"] == "done"
    assert dentist["pattern"]["name"] == "Phone calls to book things"
