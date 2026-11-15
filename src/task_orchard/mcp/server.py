"""The MCP adapter: a thin layer over core.service.Orchard.

A tool function here does three things and nothing else: accept arguments in
the names the model uses, call one Orchard method, and return its dict. No
logic, no database access, no formatting. If a tool needs more than that, the
logic belongs in core.

Tools are added phase by phase (SPEC section 8). Each one is wrapped with
`guarded`, which turns an OrchardError into a tool error the model can read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import Annotated, Any, NotRequired, TypedDict

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from task_orchard.config import Settings
from task_orchard.core.errors import OrchardError
from task_orchard.core.service import Orchard
from task_orchard.mcp.guides import Guide, load_guides

INSTRUCTIONS = """\
Task Orchard keeps one person's tasks as trees and helps them decide what to \
do with their time and energy. It stores and computes facts; you make the \
judgements: what matters, how to break work down, what to suggest.

How to work with it:
- At the start of a conversation, call get_guide with name "overview" unless \
you have already read it. It explains the concepts and lists the other guides.
- Before a multi-step job (triage, breaking a task down, planning a block of \
time, closing a task), call get_guide for that job and follow it.
- Task ids are numbers that come from tool results. Never invent one.
- Times in results are already in the user's timezone and come with a \
relative phrase such as "in 2 days". Use those; do not do date arithmetic.
- If a tool returns an error, read its hint and correct the call.
"""

TZ_WARNING = """
Note: the user's timezone could not be detected, so UTC is being assumed. \
Tell the user once that they should set TASK_ORCHARD_TZ (for example \
"Europe/London") in this server's environment.
"""


def guarded[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Report domain errors to the model as {error, message, hint} JSON."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except OrchardError as err:
            raise ToolError(json.dumps(err.to_dict())) from err

    return wrapper


def _choice(*names: str) -> Any:
    """A string whose schema lists the allowed names.

    Not Literal[...]: pydantic would reject "High" or "must " before core sees
    it, and SPEC 4.1 matches names leniently. Core validates and explains.
    """
    return Annotated[str, Field(json_schema_extra={"enum": list(names)})]


def _p(description: str) -> Any:
    return Field(description=description)


PriorityName = _choice("low", "normal", "high", "critical")
LevelName = _choice("low", "medium", "high")
MotiveName = _choice("want", "should", "must")
FirmnessName = _choice("hard", "soft")
AchievableName = _choice("yes", "no", "unknown")
SourceName = _choice("guess", "pattern", "evidence", "user")
StatusFilter = _choice("open", "closed", "any")
ClearName = _choice(
    "description", "notes", "priority", "deadline", "snooze_until", "estimate_minutes",
    "activation", "drain", "motive", "conditions", "pattern_id", "parent_id",
    "cadence", "gap_days",
)  # fmt: skip

WHEN = 'A time: "2026-11-20", "2026-11-20T17:00", "friday", "tomorrow 09:30" or "+3d".'
READ_ONLY = {"readOnlyHint": True}


class CaptureItem(TypedDict):
    title: str
    notes: NotRequired[str]


def _register_prompt(mcp: FastMCP, guide: Guide) -> None:
    def prompt() -> str:
        return guide.body

    prompt.__name__ = guide.name.replace("-", "_")
    mcp.prompt(name=guide.name, description=guide.description)(prompt)


def build_server(orchard: Orchard, settings: Settings | None = None) -> FastMCP:
    instructions = INSTRUCTIONS
    if settings is not None and not settings.timezone_detected:
        instructions += TZ_WARNING
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

    _register_task_tools(mcp, orchard)
    return mcp


def _register_task_tools(mcp: FastMCP, orchard: Orchard) -> None:
    """The task tools, in the order of SPEC section 8. Each calls one Orchard method."""

    @mcp.tool
    @guarded
    def capture_tasks(
        items: Annotated[
            list[CaptureItem], _p('Things to remember, e.g. [{"title": "Call the dentist"}].')
        ],
    ) -> dict[str, Any]:
        """Put things in the inbox with no questions asked: 1 to 20 at once.

        Use when the user mentions things to do and you should not interrupt
        them to ask for details. Captured tasks wait in the inbox until triage.
        The result flags open tasks that look like duplicates.
        """
        return orchard.capture_tasks(items)

    @mcp.tool
    @guarded
    def create_task(
        title: Annotated[str, _p('Short title, e.g. "Book the MOT".')],
        description: Annotated[str | None, _p("What the task is, e.g. the goal.")] = None,
        notes: Annotated[str | None, _p("Working notes, e.g. phone numbers.")] = None,
        parent_id: Annotated[int | None, _p("Put it under this task, e.g. 12.")] = None,
        priority: Annotated[PriorityName | None, _p('e.g. "high".')] = None,
        deadline: Annotated[str | None, _p(WHEN + " A bare day means 23:59.")] = None,
        firmness: Annotated[
            FirmnessName | None, _p('"hard" if missing it has a real cost.')
        ] = None,
        snooze_until: Annotated[str | None, _p(WHEN + " Hidden until then.")] = None,
        achievable: Annotated[AchievableName | None, _p('"yes" if it is one clear step.')] = None,
        estimate_minutes: Annotated[int | None, _p("Expected minutes of work, e.g. 30.")] = None,
        activation: Annotated[LevelName | None, _p('How hard to start, e.g. "high".')] = None,
        drain: Annotated[LevelName | None, _p('How tiring, e.g. "low".')] = None,
        motive: Annotated[MotiveName | None, _p('Who wants it done, e.g. "must".')] = None,
        conditions: Annotated[list[str] | None, _p('Needs, e.g. ["at-home"].')] = None,
        pattern_id: Annotated[int | None, _p("Kind of work, from find_patterns, e.g. 3.")] = None,
        blocked_by: Annotated[list[int] | None, _p("Must finish first, e.g. [7].")] = None,
        blocks: Annotated[list[int] | None, _p("Must wait for this one, e.g. [9].")] = None,
        sources: Annotated[
            dict[str, SourceName] | None,
            _p('Where values came from, e.g. {"deadline": "user"}. Unlisted fields are guesses.'),
        ] = None,
    ) -> dict[str, Any]:
        """Create a task with whatever is known. It skips the inbox.

        Leave out anything you do not know; never fill in a middle value. Mark
        values the user stated with sources, so they are protected from later
        guesses. Adding a task under a parent makes the parent not achievable.
        """
        return orchard.create_task(
            title, description=description, notes=notes, parent_id=parent_id,
            priority=priority, deadline=deadline, firmness=firmness,
            snooze_until=snooze_until, achievable=achievable,
            estimate_minutes=estimate_minutes, activation=activation, drain=drain,
            motive=motive, conditions=conditions, pattern_id=pattern_id,
            blocked_by=blocked_by, blocks=blocks, sources=sources,
        )  # fmt: skip

    @mcp.tool
    @guarded
    def update_task(
        task_id: Annotated[int, _p("The task to change, e.g. 12.")],
        title: Annotated[str | None, _p('New title, e.g. "Book the MOT".')] = None,
        description: Annotated[str | None, _p("New description.")] = None,
        notes: Annotated[str | None, _p("Replaces all the notes.")] = None,
        append_note: Annotated[str | None, _p('Adds a line, e.g. "Rang, no answer".')] = None,
        parent_id: Annotated[int | None, _p("Move under this task, e.g. 4.")] = None,
        priority: Annotated[PriorityName | None, _p('e.g. "high".')] = None,
        deadline: Annotated[str | None, _p(WHEN + " Keeps the current firmness.")] = None,
        firmness: Annotated[
            FirmnessName | None, _p('Alone, re-grades the deadline: "hard".')
        ] = None,
        snooze_until: Annotated[str | None, _p(WHEN)] = None,
        achievable: Annotated[AchievableName | None, _p('e.g. "yes".')] = None,
        estimate_minutes: Annotated[int | None, _p("e.g. 45.")] = None,
        activation: Annotated[LevelName | None, _p('e.g. "medium".')] = None,
        drain: Annotated[LevelName | None, _p('e.g. "high".')] = None,
        motive: Annotated[MotiveName | None, _p('e.g. "want".')] = None,
        conditions: Annotated[list[str] | None, _p('Replaces them, e.g. ["outdoors"].')] = None,
        pattern_id: Annotated[int | None, _p("e.g. 3.")] = None,
        clear: Annotated[list[ClearName] | None, _p('Fields to unset, e.g. ["deadline"].')] = None,
        triaged: Annotated[bool | None, _p("Pass false to leave it in the inbox.")] = None,
        sources: Annotated[
            dict[str, SourceName] | None, _p('e.g. {"priority": "user"} when the user said so.')
        ] = None,
    ) -> dict[str, Any]:
        """Change a task. Omitted fields stay as they are; unset with clear.

        Any update takes the task out of the inbox. Values the user set are
        kept unless you pass sources with "user" for them; the result lists
        skipped fields. Closed tasks only take description, notes and pattern.
        """
        return orchard.update_task(
            task_id, title=title, description=description, notes=notes,
            append_note=append_note, parent_id=parent_id, priority=priority,
            deadline=deadline, firmness=firmness, snooze_until=snooze_until,
            achievable=achievable, estimate_minutes=estimate_minutes,
            activation=activation, drain=drain, motive=motive, conditions=conditions,
            pattern_id=pattern_id, clear=clear, triaged=triaged, sources=sources,
        )  # fmt: skip

    @mcp.tool
    @guarded
    def link_tasks(
        blocker_id: Annotated[int, _p("Must be finished first, e.g. 7.")],
        blocked_id: Annotated[int, _p("Waits for the blocker, e.g. 9.")],
    ) -> dict[str, Any]:
        """Record that one task must be finished before another can start.

        The blocked task and everything under it wait. Links that would make
        tasks wait on each other are refused, with the loop shown.
        """
        return orchard.link_tasks(blocker_id, blocked_id)

    @mcp.tool
    @guarded
    def unlink_tasks(
        blocker_id: Annotated[int, _p("e.g. 7.")],
        blocked_id: Annotated[int, _p("e.g. 9.")],
    ) -> dict[str, Any]:
        """Remove a "must finish first" link. Works on closed tasks too."""
        return orchard.unlink_tasks(blocker_id, blocked_id)

    @mcp.tool(annotations=READ_ONLY)
    @guarded
    def get_task(task_id: Annotated[int, _p("e.g. 12.")]) -> dict[str, Any]:
        """Everything about one task: its fields and where each came from,
        children, links, recent history."""
        return orchard.get_task(task_id)

    @mcp.tool(annotations=READ_ONLY)
    @guarded
    def get_tree(
        root_id: Annotated[
            int | None, _p("Start here, e.g. 4. Omit for all top-level tasks.")
        ] = None,
        depth: Annotated[int, _p("Levels to show, 1 to 5, e.g. 2.")] = 2,
        include_closed: Annotated[bool, _p("Also show done and dropped tasks.")] = False,
    ) -> dict[str, Any]:
        """See how tasks are arranged: a tree of titles and states.

        `more` counts tasks below what is shown; call again with that task as
        root_id to see them. At most 100 tasks are shown.
        """
        return orchard.get_tree(root_id, depth, include_closed)

    @mcp.tool(annotations=READ_ONLY)
    @guarded
    def search_tasks(
        query: Annotated[str | None, _p('Words to find, e.g. "dentist". Omit for recent.')] = None,
        status: Annotated[StatusFilter, _p('"open", "closed" or "any".')] = "open",
        inbox_only: Annotated[bool, _p("Only tasks waiting for triage.")] = False,
        limit: Annotated[int, _p("1 to 30, e.g. 10.")] = 20,
    ) -> dict[str, Any]:
        """Find tasks by words in their title, description or notes.

        With no query, lists the most recently changed tasks. Use this to find
        an id before changing a task; never guess ids.
        """
        return orchard.search_tasks(query, status, inbox_only, limit)
