"""Command line entry point.

    task-orchard            Run the MCP server over stdio (same as `serve`).
    task-orchard serve      Run the MCP server over stdio.
    task-orchard init       Create or migrate the database, then exit.
    task-orchard info       Print the database path and timezone in use.
    task-orchard seed-demo  Fill an EMPTY database with demo data.

Configuration comes from the environment; see task_orchard/config.py.

When running as an MCP server, stdout belongs to the protocol. Anything meant
for a human must go to stderr.
"""

from __future__ import annotations

import argparse
import sys

from task_orchard.config import load_settings
from task_orchard.core.service import Orchard


def _serve() -> int:
    from task_orchard.mcp.server import build_server

    settings = load_settings()
    orchard = Orchard.open(settings)
    build_server(orchard, settings).run(transport="stdio", show_banner=False)
    return 0


def _init() -> int:
    settings = load_settings()
    Orchard.open(settings)
    print(f"Database ready at {settings.db_path}", file=sys.stderr)
    return 0


def _info() -> int:
    settings = load_settings()
    detected = "" if settings.timezone_detected else "  (not detected; set TASK_ORCHARD_TZ)"
    print(f"database: {settings.db_path}")
    print(f"timezone: {settings.timezone.key}{detected}")
    return 0


def _seed_demo() -> int:
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


COMMANDS = {"serve": _serve, "init": _init, "info": _info, "seed-demo": _seed_demo}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task-orchard", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", nargs="?", default="serve", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    return COMMANDS[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
