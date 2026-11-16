"""Command line entry point.

    task-orchard              Run the MCP server over stdio (same as `mcp`).
    task-orchard chat         Talk to Task Orchard's assistant in the terminal.
    task-orchard ask TEXT     One question, one answer, then exit.
    task-orchard mcp          Run the MCP server over stdio.
    task-orchard serve        The same, until Phase 6 makes it the HTTP service.
    task-orchard init         Create or migrate the database, then exit.
    task-orchard info         Print the database, timezone and models in use.
    task-orchard seed-demo    Fill an EMPTY database with demo data.

Configuration comes from the environment; see task_orchard/config.py.

When running as an MCP server, stdout belongs to the protocol. Anything meant
for a human must go to stderr. In chat and ask, stdout carries only answers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from task_orchard.config import Settings, load_settings
from task_orchard.core.clock import Clock, SystemClock
from task_orchard.core.service import Orchard


def _serve(args: argparse.Namespace) -> int:
    from task_orchard.mcp.server import build_server

    settings = load_settings()
    orchard = Orchard.open(settings)
    build_server(orchard, settings).run(transport="stdio", show_banner=False)
    return 0


def _app(args: argparse.Namespace) -> int:
    from task_orchard.agent import chat

    settings = load_settings()

    def open_orchard(settings: Settings = settings) -> tuple[Orchard, Clock]:
        clock = SystemClock()
        return Orchard.open(settings, clock), clock

    return chat.main(
        args.command,
        settings,
        open_orchard,
        text=getattr(args, "text", None),
        strategic=args.strategic,
        transcript=getattr(args, "transcript", None),
    )


def _init(args: argparse.Namespace) -> int:
    settings = load_settings()
    Orchard.open(settings)
    print(f"Database ready at {settings.db_path}", file=sys.stderr)
    return 0


def _info(args: argparse.Namespace) -> int:
    settings = load_settings()
    detected = "" if settings.timezone_detected else "  (not detected; set TASK_ORCHARD_TZ)"
    print(f"database: {settings.db_path}")
    print(f"timezone: {settings.timezone.key}{detected}")
    print(f"model, basic: {settings.model_basic}")
    print(f"model, strategic: {settings.model_strategic}")
    return 0


def _seed_demo(args: argparse.Namespace) -> int:
    from task_orchard.devtools.seed import DatabaseNotEmpty, seed_demo

    settings = load_settings()
    orchard = Orchard.open(settings)
    try:
        with orchard.tx() as tx:
            handles = seed_demo(tx.session, tx.now, tx.tz)
    except DatabaseNotEmpty as err:
        print(f"Refusing to seed: {err}", file=sys.stderr)
        return 1
    print(f"Seeded {len(handles)} demo tasks into {settings.db_path}", file=sys.stderr)
    return 0


COMMANDS = {
    "serve": _serve,
    "mcp": _serve,
    "chat": _app,
    "ask": _app,
    "init": _init,
    "info": _info,
    "seed-demo": _seed_demo,
}


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="task-orchard",
        description="Task Orchard: one person's tasks, and an assistant to help choose among them.",
    )
    sub = top.add_subparsers(dest="command", metavar="command")
    sub.add_parser("mcp", help="run the MCP server over stdio")
    sub.add_parser("serve", help="the same as mcp, until Phase 6")
    chat = sub.add_parser("chat", help="talk to Task Orchard's assistant in the terminal")
    chat.add_argument("--strategic", action="store_true", help="start on the strategic tier")
    chat.add_argument("--transcript", type=Path, help="append every message to this file")
    ask = sub.add_parser("ask", help="one question, one answer, then exit")
    ask.add_argument("text", help="what to ask")
    ask.add_argument("--strategic", action="store_true", help="use the strategic tier")
    sub.add_parser("init", help="create or migrate the database, then exit")
    sub.add_parser("info", help="print the database, timezone and models in use")
    sub.add_parser("seed-demo", help="fill an EMPTY database with demo data")
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command is None:
        args.command = "serve"
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
