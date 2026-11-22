"""Scenarios for recurring tasks (SPEC 7; the `triage` and `complete-and-reflect` guides)."""

from __future__ import annotations

from task_orchard.agent.model import BASIC
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from tests.scenarios.harness import run


def test_rent_on_the_first_becomes_a_scheduled_task(
    orchard: Orchard, clock: FixedClock, seeded
) -> None:
    result = run(orchard, clock, BASIC, "I need to pay the rent on the 1st of every month.")

    found = orchard.search_tasks("rent")["results"]
    recurring = [t for t in found if "recurring" in t]
    assert len(recurring) == 1, result.tool_calls
    task = recurring[0]
    assert task["recurring"]["mode"] == "scheduled"
    assert task["recurring"]["next_slot"]["at"].startswith("2026-04-01")
    assert "due" not in task


def test_a_run_is_recorded_against_the_flexible_task(
    orchard: Orchard, clock: FixedClock, seeded
) -> None:
    result = run(orchard, clock, BASIC, "Just got back from a run, about 40 minutes.")

    calls = result.called("complete_task")
    assert len(calls) == 1, result.tool_calls
    run_task = orchard.get_task(seeded["run"])["task"]
    assert "status" not in run_task  # the recurring task stays open
    assert run_task["recurring"]["done_count"] == 5
    assert run_task["recurring"]["last_done"] == "now"
    assert "dormant" in run_task["state"]  # asleep for its two-day gap
