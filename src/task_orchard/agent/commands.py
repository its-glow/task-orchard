"""The tester's slash commands in `task-orchard chat` (SPEC 10.2).

Each one looks at the session from outside: it never calls the model and adds
nothing to the conversation, so the model never sees it. What a command shows
goes to stdout, so it can be piped; complaints about how it was used go to stderr.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import fields
from typing import Any, TextIO

from task_orchard.agent.loop import Event, Session
from task_orchard.agent.model import Usage, cost
from task_orchard.tools import call_tool

HELP = """\
Commands:
  /new              start a fresh conversation, on the basic tier
  /strategic        use the strategic tier for the turns that follow
  /basic            use the basic tier for the turns that follow
  /help             this list
  /exit, /quit      leave (Ctrl-D does the same)
For testing. The model never sees these:
  /tools            the tools the model has, with their parameters (? = optional)
  /tool NAME [JSON] run a read-only tool and print its result, e.g.
                    /tool get_candidates {"minutes_available": 30}
  /last             the full results of the last turn's tool calls
  /prompt           the system prompt, as sent
  /usage            tokens and estimated cost of each request in this conversation"""

TOOL_USAGE = 'Usage: /tool NAME [JSON], e.g. /tool get_task {"task_id": 12}. /tools lists them.'


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


def tool(session: Session, arg: str, out: TextIO, err: TextIO) -> None:
    name, _, raw = arg.strip().partition(" ")
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


def tool_line(event: Event) -> str:
    """-> get_candidates(minutes_available=30, energy="low"), then !! code on failure."""
    arguments = event.get("input")
    if isinstance(arguments, dict):
        shown = ", ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in arguments.items()
        )
    else:
        shown = json.dumps(arguments, ensure_ascii=False)
    line = f"-> {event['name']}({shown})"
    if event.get("is_error"):
        line += f" !! {event['payload'].get('error', 'error')}"
    return line


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
