"""Scenarios: what the user says about themselves in passing is kept; moods are not."""

from __future__ import annotations

from task_orchard.agent.model import BASIC
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from tests.scenarios.harness import run


def test_a_lasting_fact_said_in_passing_is_saved(
    orchard: Orchard, clock: FixedClock, seeded
) -> None:
    result = run(
        orchard,
        clock,
        BASIC,
        "I do all my errands on Saturday mornings, so there's no point suggesting them "
        "on a weekday. What should I work on now?",
    )

    notes = result.called("write_profile_note")
    assert notes, result.tool_calls
    assert all(str(args.get("topic", "")).strip().lower() != "charter" for args in notes)
    saved = " ".join(n["text"] for n in orchard.read_profile()["notes"]).lower()
    assert "saturday" in saved, saved


def test_a_passing_mood_is_not_saved(orchard: Orchard, clock: FixedClock, seeded) -> None:
    result = run(orchard, clock, BASIC, "I'm wiped today. Anything easy I can do in 20 minutes?")

    assert result.called("get_candidates"), result.tool_calls
    assert result.writes == [], result.tool_calls
