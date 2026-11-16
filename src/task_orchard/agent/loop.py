"""The app's tool-use loop (SPEC 10.2, a turn).

A session is append-only: the system prompt, the tool list and every message
already sent go out again exactly as they were. Nothing here edits, re-orders
or drops them. The loop holds no task logic and makes no judgement: it sends
the conversation, runs the tools the model asks for through call_tool, and
repeats until the model answers or the step limit is reached.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from task_orchard.agent.model import BASIC, TIERS, Model, Reply
from task_orchard.agent.prompt import now_line, system_prompt
from task_orchard.core.clock import Clock
from task_orchard.core.service import Orchard
from task_orchard.guides import load_guides
from task_orchard.tools import Tool, call_tool, catalogue

MAX_STEPS = 12
SESSION_TOKEN_LIMIT = 150_000

STEP_LIMIT_TEXT = (
    f"I stopped after {MAX_STEPS} steps without finishing. "
    'Say "continue" to carry on, or ask in a different way.'
)
CUT_OFF_NOTE = '(The answer was cut off. Say "continue" for the rest.)'
REFUSAL_TEXT = "The model declined to answer that."
NOT_RUN = {
    "error": "not_run",
    "message": "This call was cut off and did not run.",
    "hint": "Call it again if it is still needed.",
}

Event = dict[str, Any]


@dataclass
class Session:
    orchard: Orchard
    clock: Clock
    models: Mapping[str, Model]
    tools: list[Tool]
    system: list[dict[str, Any]]
    tool_specs: list[dict[str, Any]]
    tier: str = BASIC
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Told of every message appended and every tool run: for the stderr trace
    # and the transcript. It never changes anything.
    on_event: Callable[[Event], None] | None = None
    # Input tokens of the latest request, cached or not.
    input_tokens: int = 0

    @property
    def model(self) -> Model:
        return self.models[self.tier]

    def switch(self, tier: str) -> None:
        """Use the other tier for the turns that follow. Nothing already sent changes."""
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        self.tier = tier

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.emit({"type": "message", "message": message})

    def emit(self, event: Event) -> None:
        if self.on_event is not None:
            self.on_event(event)


def start_session(
    orchard: Orchard,
    clock: Clock,
    models: Mapping[str, Model],
    *,
    tier: str = BASIC,
    timezone_detected: bool = True,
    on_event: Callable[[Event], None] | None = None,
) -> Session:
    """A new session: the profile is read now, once, and the prompt fixed."""
    tools = catalogue(orchard)
    known = timezone_detected or orchard.stored_timezone() is not None
    system = system_prompt(load_guides(), orchard.read_profile(), timezone_detected=known)
    specs = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]
    session = Session(
        orchard=orchard,
        clock=clock,
        models=models,
        tools=tools,
        system=system,
        tool_specs=specs,
        on_event=on_event,
    )
    session.switch(tier)
    return session


def run_turn(session: Session, user_text: str) -> str:
    """One turn: the user's message in, the answer out.

    ModelUnavailable from the model propagates. Whatever was appended before it,
    including the results of tools that ran, stays: those changes happened.
    """
    now = session.clock.now()
    session.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{now_line(now, session.orchard.tz)}\n{user_text}"}
            ],
        }
    )
    for _ in range(MAX_STEPS):
        reply = session.model.respond(session.system, session.tool_specs, session.messages)
        session.input_tokens = reply.input_tokens
        session.append({"role": "assistant", "content": reply.content})
        calls = [block for block in reply.content if block.get("type") == "tool_use"]
        if reply.stop != "tool_use" or not calls:
            if calls:
                # The API rejects a tool call with no result after it, and the call
                # cannot be removed, so answer each one as not run.
                session.append(
                    {"role": "user", "content": [_result(c["id"], NOT_RUN, True) for c in calls]}
                )
            return _answer(reply)
        results = []
        for call in calls:
            outcome = call_tool(session.tools, call["name"], call.get("input"))
            session.emit(
                {
                    "type": "tool",
                    "name": call["name"],
                    "input": call.get("input"),
                    "is_error": outcome.is_error,
                    "payload": outcome.payload,
                }
            )
            results.append(_result(call["id"], outcome.payload, outcome.is_error))
        # All the results of one reply go back in one message.
        session.append({"role": "user", "content": results})
    return STEP_LIMIT_TEXT


def _result(tool_use_id: str, payload: dict[str, Any], is_error: bool) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(payload, ensure_ascii=False),
    }
    if is_error:
        block["is_error"] = True
    return block


def reply_text(reply: Reply) -> str:
    texts = [block.get("text", "") for block in reply.content if block.get("type") == "text"]
    return "\n\n".join(text.strip() for text in texts if text.strip())


def _answer(reply: Reply) -> str:
    if reply.stop == "refusal":
        return REFUSAL_TEXT
    text = reply_text(reply)
    if reply.stop == "max_tokens":
        return f"{text}\n\n{CUT_OFF_NOTE}" if text else CUT_OFF_NOTE
    return text
