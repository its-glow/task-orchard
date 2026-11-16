"""The seed matches what roll_forward would have written (SPEC 11, Phase 4)."""

from __future__ import annotations

from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from tests.helpers import events, last_event_id


def test_a_request_on_the_seed_rolls_nothing_forward(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    since = last_event_id(orchard)
    orchard.get_critical()
    assert events(orchard, since) == []


def test_the_seeded_summary_spawns_next_friday_on_schedule(
    orchard: Orchard, clock: FixedClock, seeded: dict[str, int]
) -> None:
    summary = seeded["summary"]
    upcoming = orchard.get_task(summary)["task"]["recurring"]["next_slot"]
    since = last_event_id(orchard)
    clock.advance(days=7)
    orchard.get_critical()
    kinds = [(e[0], e[2].get("occurrence_at")) for e in events(orchard, since)]
    assert ("spawned", upcoming["at"]) in kinds
