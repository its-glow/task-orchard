"""The tool catalogue: the one place a tool is defined (SPEC 10.1).

A tool is one operation of SPEC section 8 as a model sees it: a name, a
description and a parameter schema. The app (agent/) and the MCP adapter (mcp/)
are both built from this list, so a tool is described once and the two cannot
drift.

A tool function does three things and nothing else: accept arguments in the
names the model uses, call one Orchard method, and return its dict. No logic,
no database access, no formatting. If a tool needs more than that, the logic
belongs in core.

call_tool is how the app runs a tool. It never raises for a failure the model
could cause: an OrchardError, an unknown tool or arguments that do not fit the
schema come back as the error JSON of SPEC 4.2, marked is_error.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, NotRequired, TypedDict, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from task_orchard.core.errors import Invalid, OrchardError
from task_orchard.core.service import Orchard


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


class CaptureItem(TypedDict):
    title: str
    notes: NotRequired[str]


class ChildItem(TypedDict):
    title: Annotated[str, Field(description='e.g. "Draft the outline".')]
    description: NotRequired[str]
    estimate_minutes: NotRequired[int]
    achievable: NotRequired[AchievableName]
    priority: NotRequired[PriorityName]
    activation: NotRequired[LevelName]
    drain: NotRequired[LevelName]
    conditions: NotRequired[list[str]]
    after: NotRequired[
        Annotated[
            list[int],
            Field(description="Positions (from 1) of children in this call to finish first."),
        ]
    ]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    fn: Callable[..., dict[str, Any]]
    # Validates arguments before fn is called; built from fn's signature.
    arguments: type[BaseModel] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Outcome:
    payload: dict[str, Any]
    is_error: bool


def catalogue(orchard: Orchard) -> list[Tool]:
    """Every tool built so far, in the order of SPEC section 8.

    The order is fixed: the app sends the list in this order on every request.
    """
    fns: list[tuple[Callable[..., dict[str, Any]], bool]] = []

    def _tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        fns.append((fn, False))
        return fn

    def _read_only(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        fns.append((fn, True))
        return fn

    @_tool
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

    @_tool
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

    @_tool
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

    @_tool
    def subdivide_task(
        parent_id: Annotated[int, _p("The task to break down, e.g. 12.")],
        children: Annotated[
            list[ChildItem],
            _p('1 to 12 steps, e.g. [{"title": "Pick a date"}, {"title": "Book", "after": [1]}].'),
        ],
        sequential: Annotated[bool, _p("True if each step waits for the one before.")] = False,
        sources: Annotated[
            dict[str, SourceName] | None,
            _p('Applies to every child, e.g. {"estimate_minutes": "user"}.'),
        ] = None,
    ) -> dict[str, Any]:
        """Break a task into its next layer of steps, in one call.

        Use when a task is not achievable as it stands. Break down one layer
        only; a step that is still vague can be broken down later. Use
        sequential or after only where the order is real. The parent becomes
        not achievable.
        """
        return orchard.subdivide_task(parent_id, children, sequential, sources)

    @_tool
    def link_tasks(
        blocker_id: Annotated[int, _p("Must be finished first, e.g. 7.")],
        blocked_id: Annotated[int, _p("Waits for the blocker, e.g. 9.")],
    ) -> dict[str, Any]:
        """Record that one task must be finished before another can start.

        The blocked task and everything under it wait. Links that would make
        tasks wait on each other are refused, with the loop shown.
        """
        return orchard.link_tasks(blocker_id, blocked_id)

    @_tool
    def unlink_tasks(
        blocker_id: Annotated[int, _p("e.g. 7.")],
        blocked_id: Annotated[int, _p("e.g. 9.")],
    ) -> dict[str, Any]:
        """Remove a "must finish first" link. Works on closed tasks too."""
        return orchard.unlink_tasks(blocker_id, blocked_id)

    @_tool
    def start_task(
        task_id: Annotated[int, _p("The task being started, e.g. 12.")],
    ) -> dict[str, Any]:
        """Record that the user is starting work on a task now.

        Starts without a finish are a signal that something is hard to start,
        so record each one. It does not change the task.
        """
        return orchard.start_task(task_id)

    @_tool
    def complete_task(
        task_id: Annotated[int | None, _p("The task that was done, e.g. 12.")] = None,
        title: Annotated[
            str | None, _p('Instead of task_id, for work never tracked: "Fixed the gate".')
        ] = None,
        parent_id: Annotated[int | None, _p("With title: file it under this task, e.g. 4.")] = None,
        motive: Annotated[MotiveName | None, _p('With title: e.g. "must".')] = None,
        description: Annotated[str | None, _p("With title: what was done.")] = None,
        completed_at: Annotated[str | None, _p(WHEN + " Omit for now.")] = None,
        actual_minutes: Annotated[int | None, _p("How long it took, e.g. 40.")] = None,
        felt_activation: Annotated[LevelName | None, _p('How hard to start: "high".')] = None,
        felt_drain: Annotated[LevelName | None, _p('How tiring it was: "low".')] = None,
        reflection: Annotated[str | None, _p("The user's words on how it went.")] = None,
        pattern_id: Annotated[int | None, _p("File under this kind of work, e.g. 3.")] = None,
        new_pattern: Annotated[
            str | None, _p('Or under a new kind of work: "Phone calls to book things".')
        ] = None,
        drop_open_children: Annotated[
            bool, _p("True to drop unfinished steps under it as well.")
        ] = False,
    ) -> dict[str, Any]:
        """Record that something was done, with how it went.

        Pass task_id for an existing task, or title for work that was never a
        task. Fill actual_minutes and the felt values from the user's words.
        The result may carry parent_check (the parent has nothing left open)
        and unblocked (tasks now free to start).
        """
        return orchard.complete_task(
            task_id, title=title, parent_id=parent_id, motive=motive,
            description=description, completed_at=completed_at,
            actual_minutes=actual_minutes, felt_activation=felt_activation,
            felt_drain=felt_drain, reflection=reflection, pattern_id=pattern_id,
            new_pattern=new_pattern, drop_open_children=drop_open_children,
        )  # fmt: skip

    @_tool
    def snooze_task(
        task_id: Annotated[int, _p("e.g. 12.")],
        until: Annotated[str | None, _p(WHEN + " A bare day means its start.")] = None,
        days: Annotated[int | None, _p("Or whole days from now, e.g. 3. 0 wakes it.")] = None,
        reason: Annotated[str | None, _p('Why, in the user\'s words: "not today".')] = None,
    ) -> dict[str, Any]:
        """The user is putting a task off: hide it until a time.

        Pass until or days. Each snooze is counted as a deferral. A time that
        is not in the future (days=0) wakes the task instead.
        """
        return orchard.snooze_task(task_id, until, days, reason)

    @_tool
    def drop_tasks(
        task_ids: Annotated[list[int], _p("1 to 50 tasks, e.g. [12, 15].")],
        reason: Annotated[str, _p('Why, e.g. "no longer needed".')],
    ) -> dict[str, Any]:
        """Close tasks that will not be done, with everything under them.

        Only when the user decided it. Already closed tasks are skipped. The
        result lists what else was dropped, parents with nothing left open,
        and tasks now free to start.
        """
        return orchard.drop_tasks(task_ids, reason)

    @_tool
    def reopen_task(task_id: Annotated[int, _p("e.g. 12.")]) -> dict[str, Any]:
        """Undo a completion or a drop: the task is open again.

        Its completion record is removed. Reopen a closed parent first.
        """
        return orchard.reopen_task(task_id)

    @_read_only
    def get_task(task_id: Annotated[int, _p("e.g. 12.")]) -> dict[str, Any]:
        """Everything about one task: its fields and where each came from,
        children, links, recent history."""
        return orchard.get_task(task_id)

    @_read_only
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

    @_read_only
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

    @_read_only
    def get_candidates(
        limit: Annotated[int, _p("How many options at most, 1 to 30, e.g. 20.")] = 20,
        under_id: Annotated[int | None, _p("Only this task and what is under it, e.g. 4.")] = None,
        min_priority: Annotated[
            PriorityName | None, _p('Only this important or more: "high".')
        ] = None,
        due_within_days: Annotated[float | None, _p("Only overdue or due within, e.g. 7.")] = None,
        minutes_available: Annotated[int | None, _p("Skip longer tasks, e.g. 45.")] = None,
        exclude_conditions: Annotated[
            list[str] | None, _p('Skip tasks needing, e.g. ["at-home"].')
        ] = None,
        energy: Annotated[
            LevelName | None, _p('The user\'s energy now, e.g. "low". Marks, never hides.')
        ] = None,
        reserve_wants: Annotated[
            int, _p("Places kept for things the user wants to do, 0 to 10.")
        ] = 3,
    ) -> dict[str, Any]:
        """What could the user work on now? A shortlist of tasks that are free to start.

        Rows come in groups (pressing, due_soon, other) and in a plain order;
        you rank them. `pool` and `filtered` say what was left out. Inbox tasks
        never appear: `inbox` counts them. For "is anything urgent?" use
        get_critical, which also shows blocked work.
        """
        return orchard.get_candidates(
            limit, under_id, min_priority, due_within_days, minutes_available,
            exclude_conditions, energy, reserve_wants,
        )  # fmt: skip

    @_read_only
    def get_critical(
        horizon_days: Annotated[
            float, _p("Also list what is due within this many days, e.g. 1.")
        ] = 1,
    ) -> dict[str, Any]:
        """What is urgent, including work that is blocked, snoozed or in the inbox.

        Lists single steps that are overdue, at risk of missing a deadline,
        due soon, or critical, each with what stands in its way.
        """
        return orchard.get_critical(horizon_days)

    @_read_only
    def find_patterns(
        query: Annotated[str | None, _p('Words, e.g. "phone call". Omit for recent.')] = None,
        limit: Annotated[int, _p("1 to 10, e.g. 5.")] = 5,
    ) -> dict[str, Any]:
        """Find kinds of work the user has done before, with what was learned.

        Use when filling in a task's fields or filing a completion. With no
        query, lists the most recently used.
        """
        return orchard.find_patterns(query, limit)

    @_tool
    def save_pattern(
        pattern_id: Annotated[int | None, _p("Pattern to change, e.g. 3. Omit to create.")] = None,
        name: Annotated[str | None, _p('e.g. "Phone calls to book things".')] = None,
        description: Annotated[str | None, _p("What counts as this kind of work.")] = None,
        learned: Annotated[
            str | None, _p('The whole note, at most 600 characters: "Takes twice the estimate."')
        ] = None,
        merge_from_id: Annotated[
            int | None, _p("Fold this pattern into pattern_id, e.g. 5.")
        ] = None,
    ) -> dict[str, Any]:
        """Create or change a kind of work, or merge two.

        learned replaces the whole note: rewrite it to fit, never append. When
        merging, pass the combined learned note in the same call.
        """
        return orchard.save_pattern(pattern_id, name, description, learned, merge_from_id)

    @_read_only
    def read_profile() -> dict[str, Any]:
        """The user's charter and what is known about them. Read at the start of a
        conversation. When the charter and your instincts disagree, follow it."""
        return orchard.read_profile()

    @_tool
    def write_profile_note(
        topic: Annotated[str, _p('One word or two, e.g. "energy". "charter" is the charter.')],
        text: Annotated[str, _p('The whole note, e.g. "Low energy after 3pm." Blank deletes it.')],
    ) -> dict[str, Any]:
        """Save something true of the user in general, so it is never asked again.

        Replaces the whole note for that topic. Notes are short: 1,000
        characters each, 8,000 in all; consolidate when full.
        """
        return orchard.write_profile_note(topic, text)

    return [_build(fn, read_only) for fn, read_only in fns]


def _build(fn: Callable[..., dict[str, Any]], read_only: bool) -> Tool:
    hints = get_type_hints(fn, include_extras=True)
    fields: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (hints[name], default)
    model = create_model(
        f"{fn.__name__}_arguments", __config__=ConfigDict(extra="forbid"), **fields
    )
    return Tool(
        name=fn.__name__,
        description=inspect.getdoc(fn) or "",
        input_schema=_clean(model.model_json_schema()),
        read_only=read_only,
        fn=fn,
        arguments=model,
    )


def _clean(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline $defs and drop pydantic's generated titles.

    A small model reads a schema more easily with no references to follow, and
    the titles only repeat the names. This matches what FastMCP shows MCP clients.
    """
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref", "")
            if ref.startswith("#/$defs/"):
                return walk(defs[ref.removeprefix("#/$defs/")])
            return {
                key: walk(value)
                for key, value in node.items()
                if key != "$defs" and not (key == "title" and isinstance(value, str))
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


def call_tool(tools: Iterable[Tool], name: str, arguments: Any) -> Outcome:
    """Run one tool for a model. Failures the model could cause come back as is_error."""
    by_name = {tool.name: tool for tool in tools}
    tool = by_name.get(name)
    if tool is None:
        return _invalid(
            f"There is no tool named {name!r}.",
            hint=f"Use one of: {', '.join(by_name)}.",
        )
    if not isinstance(arguments, dict):
        return _invalid(
            f"The arguments to {name} must be an object.",
            hint="Pass the arguments as an object of parameter names and values, e.g. {}.",
        )
    try:
        parsed = tool.arguments.model_validate(arguments)
    except ValidationError as err:
        return _invalid(
            f"The arguments to {name} do not fit its parameters.",
            hint=_hint(tool, err),
        )
    kwargs = {key: getattr(parsed, key) for key in parsed.model_fields_set}
    try:
        return Outcome(tool.fn(**kwargs), is_error=False)
    except OrchardError as err:
        return Outcome(err.to_dict(), is_error=True)


def _invalid(message: str, *, hint: str) -> Outcome:
    return Outcome(Invalid(message, hint=hint).to_dict(), is_error=True)


def _hint(tool: Tool, err: ValidationError) -> str:
    properties = tool.input_schema.get("properties", {})
    problems: list[str] = []
    for error in err.errors():
        loc = error["loc"]
        param = str(loc[0]) if loc else ""
        where = ".".join(str(part) for part in loc)
        if error["type"] == "extra_forbidden":
            problems.append(
                f"{param!r} is not a parameter of {tool.name}; "
                f"its parameters are {', '.join(properties)}."
            )
            continue
        described = properties.get(param, {}).get("description", "")
        if error["type"] == "missing" and len(loc) == 1:
            problems.append(f"{param!r} is required: {described}")
        else:
            problems.append(f"{where!r}: {error['msg']}. {described}".strip())
    return " ".join(problems) + " Correct the call and try again."
