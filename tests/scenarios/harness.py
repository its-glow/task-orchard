"""Running a scenario: the real prompt and tools, a real model, the demo data (SPEC 10.2).

A scenario asserts on what was called and on the end state, never on wording.
Each one names its tier and runs on that tier's setting, so by default on
claude-haiku-5-5. Set TASK_ORCHARD_MODEL_BASIC or TASK_ORCHARD_MODEL_STRATEGIC
to try a different model.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from task_orchard.agent.chat import models_for
from task_orchard.agent.loop import Event, Session, run_turn, start_session
from task_orchard.config import load_settings
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard


@dataclass
class Run:
    session: Session
    answers: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    @property
    def tool_calls(self) -> list[tuple[str, Any]]:
        return [(e["name"], e["input"]) for e in self.events if e["type"] == "tool"]

    def called(self, name: str) -> list[dict[str, Any]]:
        return [arguments for tool, arguments in self.tool_calls if tool == name]

    @property
    def writes(self) -> list[str]:
        read_only = {t.name for t in self.session.tools if t.read_only}
        return [name for name, _ in self.tool_calls if name not in read_only]


def run(orchard: Orchard, clock: FixedClock, tier: str, *messages: str) -> Run:
    models = models_for(load_settings())
    events: list[Event] = []
    session = start_session(orchard, clock, models, tier=tier, on_event=events.append)
    result = Run(session=session, events=events)
    for message in messages:
        result.answers.append(run_turn(session, message))
    _report(result, models[tier])
    return result


def _report(result: Run, model: Any) -> None:
    """Printed for the person running scenarios (pytest -m scenario -s)."""
    print(f"\n--- {model.model_id} ({result.session.tier})", file=sys.stderr)
    for name, arguments in result.tool_calls:
        print(f"  -> {name}({arguments})", file=sys.stderr)
    for answer in result.answers:
        print(f"  answer: {answer}", file=sys.stderr)
    usage = [e for e in result.events if e["type"] == "usage"]
    for i, figures in enumerate(usage, start=1):
        print(f"  request {i}: {figures}", file=sys.stderr)
