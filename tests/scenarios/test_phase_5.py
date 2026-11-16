"""Scenarios for the Phase 5 guides: onboarding, plan-time-block, housekeeping
and weekly-review (SPEC 9, 10.2, and the Phase 5 note in section 11)."""

from __future__ import annotations

from task_orchard.agent.model import BASIC, STRATEGIC
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from tests.scenarios.harness import run

DECISIONS = {"drop_tasks", "complete_task", "merge_tasks"}


def test_onboarding_saves_a_charter_and_the_timezone(orchard: Orchard, clock: FixedClock) -> None:
    result = run(
        orchard,
        clock,
        STRATEGIC,
        "Hi, I'm new here. Can you get me set up?",
        "Mostly I want work to stop eating my evenings, and to get back to painting a "
        "couple of times a week.",
        "Yes, that's right, save it.",
        "Yes, that's the right time. I'm in London.",
    )

    charter = orchard.read_profile().get("charter")
    assert charter and "paint" in charter["text"].lower(), result.tool_calls
    assert orchard.stored_timezone() == "Europe/London", result.tool_calls


def test_a_two_hour_block_is_planned_without_changing_anything(
    orchard: Orchard, clock: FixedClock, seeded
) -> None:
    result = run(
        orchard,
        clock,
        STRATEGIC,
        "I've got two hours this afternoon and decent energy. Plan it for me.",
    )

    calls = result.called("get_candidates")
    assert calls, result.tool_calls
    assert int(calls[0].get("minutes_available", 0)) == 120
    assert not set(result.writes) - {"start_task"}, result.writes


def test_housekeeping_merges_an_exact_duplicate_and_drops_nothing(
    orchard: Orchard, clock: FixedClock, seeded
) -> None:
    extra = orchard.create_task("Buy milk")["task"]["id"]
    result = run(orchard, clock, BASIC, "Can you do some housekeeping?")

    merges = result.called("merge_tasks")
    assert len(merges) == 1, result.tool_calls
    assert {int(merges[0]["source_id"]), int(merges[0]["into_id"])} == {extra, seeded["milk"]}
    assert "drop_tasks" not in result.writes
    assert "complete_task" not in result.writes


def test_the_weekly_review_reads_the_week_and_decides_nothing_alone(
    orchard: Orchard, clock: FixedClock, seeded
) -> None:
    result = run(orchard, clock, STRATEGIC, "Let's do my weekly review.")

    assert result.called("get_recent_activity"), result.tool_calls
    assert result.called("get_review_queue"), result.tool_calls
    assert not DECISIONS & set(result.writes), result.writes


def test_triage_fills_in_the_inbox(orchard: Orchard, clock: FixedClock, seeded) -> None:
    """The `triage` guide (Phase 2). The inbox holds a standing desk, a reply to
    Priya, and running twice a week, which repeats the recurring "Go for a run"."""
    result = run(
        orchard,
        clock,
        BASIC,
        "Triage my inbox.",
        "Go with your best guesses for anything you'd ask me about.",
    )

    for handle in ("inbox_desk", "inbox_reply"):
        task = orchard.get_task(seeded[handle])["task"]
        assert "inbox" not in task.get("state", []), (handle, result.tool_calls)
    assert len(result.called("update_task")) >= 2, result.tool_calls
    assert "drop_tasks" not in result.writes
