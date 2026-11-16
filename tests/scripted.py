"""ScriptedModel: the app's model in tests (SPEC 10.2). It never calls a network.

It returns replies from a list and records a deep copy of every request, so a
test can check what was sent at each step even though the session's message
list keeps growing.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from task_orchard.agent.model import Reply


@dataclass
class Request:
    system: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    messages: list[dict[str, Any]]


@dataclass
class ScriptedModel:
    replies: list[Reply | Exception]
    model_id: str = "scripted"
    requests: list[Request] = field(default_factory=list)

    def respond(
        self,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> Reply:
        self.requests.append(Request(*(copy.deepcopy(x) for x in (system, tools, messages))))
        if not self.replies:
            raise AssertionError("ScriptedModel ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


_ids = iter(range(1, 1_000_000))


def text(answer: str, *, stop: str = "end", tokens: int = 1000) -> Reply:
    return Reply(content=[{"type": "text", "text": answer}], stop=stop, input_tokens=tokens)  # type: ignore[arg-type]


def calls(*pairs: tuple[str, Any], stop: str = "tool_use", tokens: int = 1000) -> Reply:
    """A reply asking for tool calls, each (name, input)."""
    content: list[dict[str, Any]] = [
        {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": arguments}
        for name, arguments in pairs
    ]
    return Reply(content=content, stop=stop, input_tokens=tokens)  # type: ignore[arg-type]


def results(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool_result blocks of a user message."""
    assert message["role"] == "user"
    return [b for b in message["content"] if b.get("type") == "tool_result"]
