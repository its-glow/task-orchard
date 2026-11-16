"""The MCP adapter: Task Orchard's tools for outside assistants (SPEC 10.3).

It is generic. Every tool comes from the catalogue in task_orchard.tools, with
the catalogue's name, description and read-only flag, so the adapter keeps up
with later phases at no cost. What is specific to MCP lives here: the server
instructions, the get_guide tool and one prompt per guide. Each tool is wrapped
with `guarded`, which turns an OrchardError into a tool error whose text is the
JSON of SPEC 4.2.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from task_orchard.config import TIMEZONE_WARNING, Settings
from task_orchard.core.errors import OrchardError
from task_orchard.core.service import Orchard
from task_orchard.guides import Guide, load_guides
from task_orchard.tools import catalogue

INSTRUCTIONS = """\
Task Orchard keeps one person's tasks as trees and helps them decide what to \
do with their time and energy. It stores and computes facts; you make the \
judgements: what matters, how to break work down, what to suggest.

How to work with it:
- At the start of a conversation, call read_profile: it holds the user's \
charter (what they want from Task Orchard) and what is known about them. Then \
call get_guide with name "overview" unless you have already read it. It \
explains the concepts and lists the other guides.
- Before a multi-step job (triage, breaking a task down, planning a block of \
time, closing a task), call get_guide for that job and follow it.
- Task ids are numbers that come from tool results. Never invent one.
- Times in results are already in the user's timezone and come with a \
relative phrase such as "in 2 days". Use those; do not do date arithmetic.
- If a tool returns an error, read its hint and correct the call.
"""

READ_ONLY = {"readOnlyHint": True}


def guarded[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Report domain errors to the model as {error, message, hint} JSON."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except OrchardError as err:
            raise ToolError(json.dumps(err.to_dict())) from err

    return wrapper


def _register_prompt(mcp: FastMCP, guide: Guide) -> None:
    def prompt() -> str:
        return guide.body

    prompt.__name__ = guide.name.replace("-", "_")
    mcp.prompt(name=guide.name, description=guide.description)(prompt)


def build_server(orchard: Orchard, settings: Settings | None = None) -> FastMCP:
    instructions = INSTRUCTIONS
    if settings is not None and not settings.timezone_detected:
        instructions += "\n" + TIMEZONE_WARNING + "\n"
    mcp = FastMCP(name="Task Orchard", instructions=instructions)
    guides = load_guides()

    @mcp.tool
    @guarded
    def get_guide(name: str | None = None) -> dict[str, Any]:
        """Read one of Task Orchard's guides: step-by-step instructions for a job.

        Call with no name to list the guides. Call with a name to get its text,
        then follow it.
        """
        if name is None:
            return {
                "guides": [{"name": g.name, "description": g.description} for g in guides.values()]
            }
        guide = guides.get(name.strip().lower())
        if guide is None:
            known = ", ".join(guides) or "(none)"
            raise ToolError(
                json.dumps(
                    {
                        "error": "not_found",
                        "message": f"There is no guide named {name!r}.",
                        "hint": f"Available guides: {known}.",
                    }
                )
            )
        return {"name": guide.name, "description": guide.description, "text": guide.body}

    for guide in guides.values():
        _register_prompt(mcp, guide)

    for tool in catalogue(orchard):
        mcp.tool(
            guarded(tool.fn),
            name=tool.name,
            description=tool.description,
            annotations=READ_ONLY if tool.read_only else None,
        )
    return mcp
