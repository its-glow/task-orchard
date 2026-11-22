"""The commands of the app's front ends: one registry for `chat` and the Discord bot.

Each command is defined once, in COMMANDS (SPEC 10.5). `chat` dispatches
through it and builds /help from it; the Discord bot registers every command
that is not terminal-only as a slash command. Adding an entry adds the command
to both.

The tester commands (SPEC 10.2) look at the session from outside: they never
call the model and add nothing to the conversation, so the model never sees
them. A handler writes what it shows to `out` and complaints about how it was
used, and notices such as the tier in use, to `err`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, Protocol, TextIO

from task_orchard.agent.loop import Event, Session
from task_orchard.agent.model import BASIC, STRATEGIC, Usage, cost
from task_orchard.agent.trace import Trace, tool_line
from task_orchard.tools import call_tool

TOOL_USAGE = (
    'Usage: /tool NAME [ARGUMENTS], e.g. /tool get_task {"task_id": 12}. /tools lists them.'
)


class Host(Protocol):
    """The front end a command acts on: its current conversation and trace."""

    # True in the terminal: /help then lists the terminal-only commands too.
    terminal: bool

    @property
    def session(self) -> Session: ...

    @property
    def trace(self) -> Trace: ...

    def new_session(self, tier: str) -> None:
        """End the session and start another on `tier`, which reads the profile again."""
        ...

    def leave(self) -> None:
        """Stop the front end. Only terminal-only commands call it."""
        ...


Args = Mapping[str, str]
Handler = Callable[[Host, Args, TextIO, TextIO], None]


@dataclass(frozen=True)
class Param:
    name: str
    description: str  # at most 100 characters: Discord's limit
    required: bool = False
    # Values to offer as the user types, where the front end can.
    complete: Callable[[Host], list[str]] | None = None


@dataclass(frozen=True)
class Command:
    name: str  # without the slash
    summary: str  # one line, at most 100 characters: Discord's limit
    run: Handler
    params: tuple[Param, ...] = ()
    aliases: tuple[str, ...] = ()
    terminal_only: bool = False
    # Listed under "For testing" in /help.
    testing: bool = False
    # A second line for /help, such as an example.
    example: str = ""

    @property
    def usage(self) -> str:
        names = [f"/{n}" for n in (self.name, *self.aliases)]
        shown = ", ".join(names)
        for p in self.params:
            shown += f" {p.name.upper()}" if p.required else f" [{p.name.upper()}]"
        return shown


def parse_args(command: Command, rest: str) -> dict[str, str]:
    """A command line's parameters: each but the last up to a space, the last to the end."""
    args: dict[str, str] = {}
    remaining = rest.strip()
    for i, param in enumerate(command.params):
        if not remaining:
            break
        if i == len(command.params) - 1:
            args[param.name] = remaining
        else:
            value, _, remaining = remaining.partition(" ")
            args[param.name] = value
            remaining = remaining.strip()
    return args


def find(name: str) -> Command | None:
    """The command called `name` (with or without its slash), or None."""
    name = name.removeprefix("/")
    for command in COMMANDS:
        if name == command.name or name in command.aliases:
            return command
    return None


def help_text(*, terminal: bool) -> str:
    shown = [c for c in COMMANDS if terminal or not c.terminal_only]
    width = max(len(c.usage) for c in shown) + 2
    lines = ["Commands:"]

    def add(command: Command) -> None:
        lines.append(f"  {command.usage.ljust(width)}{command.summary}")
        if command.example:
            lines.append(f"  {'':{width}}{command.example}")

    for command in shown:
        if not command.testing:
            add(command)
    lines.append("For testing. The model never sees these:")
    for command in shown:
        if command.testing:
            add(command)
    return "\n".join(lines)


def announce(session: Session, err: TextIO) -> None:
    """[basic: claude-haiku-5-5]"""
    model = getattr(session.model, "model_id", type(session.model).__name__)
    print(f"[{session.tier}: {model}]", file=err, flush=True)


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def tools(session: Session, out: TextIO) -> None:
    for tool in session.tools:
        schema = tool.input_schema
        required = set(schema.get("required", []))
        params = [p if p in required else f"{p}?" for p in schema.get("properties", {})]
        mark = "  [read-only]" if tool.read_only else ""
        summary = " ".join(tool.description.strip().split("\n\n")[0].split())
        print(f"{tool.name}({', '.join(params)}){mark}\n    {summary}", file=out)
    out.flush()


def tool(session: Session, name: str, raw: str, out: TextIO, err: TextIO) -> None:
    if not name:
        print(TOOL_USAGE, file=err, flush=True)
        return
    by_name = {t.name: t for t in session.tools}
    found = by_name.get(name)
    if found is None:
        print(f"There is no tool named {name}. {TOOL_USAGE}", file=err, flush=True)
        return
    if not found.read_only:
        readable = ", ".join(t.name for t in session.tools if t.read_only)
        print(f"{name} changes tasks; only read-only tools run here: {readable}.", file=err)
        err.flush()
        return
    arguments: Any = {}
    if raw.strip():
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"The arguments are not valid JSON ({e.msg}). {TOOL_USAGE}", file=err)
            err.flush()
            return
    outcome = call_tool(session.tools, name, arguments)
    print(_json(outcome.payload), file=out, flush=True)


def last(turn_tools: Sequence[Event], out: TextIO, err: TextIO) -> None:
    if not turn_tools:
        print("No tool calls in the last turn.", file=err, flush=True)
        return
    for event in turn_tools:
        print(tool_line(event), file=out)
        print(_json(event["payload"]), file=out)
    out.flush()


def prompt(session: Session, out: TextIO) -> None:
    for i, block in enumerate(session.system, start=1):
        cached = " (cached)" if i == 1 and len(session.system) > 1 else ""
        print(f"----- system block {i}{cached} -----", file=out)
        print(block["text"], file=out)
    print(
        f"----- and {len(session.tool_specs)} tools, before the system prompt (/tools) -----",
        file=out,
        flush=True,
    )


_COLUMNS = [f.name for f in fields(Usage)]


def usage(requests: Sequence[Event], out: TextIO, err: TextIO) -> None:
    if not requests:
        print("No requests in this conversation yet.", file=err, flush=True)
        return
    rows: list[list[str]] = []
    totals = dict.fromkeys(_COLUMNS, 0)
    spent, priced = 0.0, True
    for i, event in enumerate(requests, start=1):
        figures = Usage(**{c: event.get(c, 0) for c in _COLUMNS})
        dollars = cost(event["model"], figures)
        for c in _COLUMNS:
            totals[c] += getattr(figures, c)
        if dollars is None:
            priced = False
        else:
            spent += dollars
        rows.append(
            [str(i), event["model"], *(f"{getattr(figures, c):,}" for c in _COLUMNS), _usd(dollars)]
        )
    rows.append(
        ["", "total", *(f"{totals[c]:,}" for c in _COLUMNS), _usd(spent if priced else None)]
    )
    header = ["#", "model", *_COLUMNS, "cost"]
    widths = [max(len(r[k]) for r in [header, *rows]) for k in range(len(header))]
    for r in [header, *rows]:
        cells = [r[0].rjust(widths[0]), r[1].ljust(widths[1])]
        cells += [cell.rjust(w) for cell, w in zip(r[2:], widths[2:], strict=True)]
        print("  ".join(cells), file=out)
    print(
        "Costs are estimates at list prices. input = tokens neither read from nor written "
        "to the cache.",
        file=out,
        flush=True,
    )


def _usd(dollars: float | None) -> str:
    return "?" if dollars is None else f"${dollars:.4f}"


# --- the registry ---


def _new(host: Host, args: Args, out: TextIO, err: TextIO) -> None:
    host.new_session(BASIC)
    announce(host.session, err)


def _tier(tier: str) -> Handler:
    def run(host: Host, args: Args, out: TextIO, err: TextIO) -> None:
        host.session.switch(tier)
        announce(host.session, err)

    return run


def _help(host: Host, args: Args, out: TextIO, err: TextIO) -> None:
    print(help_text(terminal=host.terminal), file=err, flush=True)


def _leave(host: Host, args: Args, out: TextIO, err: TextIO) -> None:
    host.leave()


def _read_only_tools(host: Host) -> list[str]:
    return [t.name for t in host.session.tools if t.read_only]


COMMANDS: tuple[Command, ...] = (
    Command("new", "start a fresh conversation, on the basic tier", _new),
    Command("strategic", "use the strategic tier for the turns that follow", _tier(STRATEGIC)),
    Command("basic", "use the basic tier for the turns that follow", _tier(BASIC)),
    Command("help", "this list", _help),
    Command("exit", "leave (Ctrl-D does the same)", _leave, aliases=("quit",), terminal_only=True),
    Command(
        "tools",
        "the tools the model has, with their parameters (? = optional)",
        lambda host, args, out, err: tools(host.session, out),
        testing=True,
    ),
    Command(
        "tool",
        "run a read-only tool and print its result",
        lambda host, args, out, err: tool(
            host.session, args.get("name", ""), args.get("arguments", ""), out, err
        ),
        params=(
            Param("name", "the tool to run; /tools lists them", True, _read_only_tools),
            Param("arguments", 'its arguments as a JSON object, e.g. {"minutes_available": 30}'),
        ),
        testing=True,
        example='e.g. /tool get_candidates {"minutes_available": 30}',
    ),
    Command(
        "last",
        "the full results of the last turn's tool calls",
        lambda host, args, out, err: last(host.trace.turn_tools, out, err),
        testing=True,
    ),
    Command(
        "prompt",
        "the system prompt, as sent",
        lambda host, args, out, err: prompt(host.session, out),
        testing=True,
    ),
    Command(
        "usage",
        "tokens and estimated cost of each request in this conversation",
        lambda host, args, out, err: usage(host.trace.requests, out, err),
        testing=True,
    ),
)
