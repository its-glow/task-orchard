# Task Orchard

A task and time management service for one person, used through an AI assistant over MCP.

You cultivate trees of tasks: big vague efforts, one-line reminders, recurring chores, things you want to do more of. In return the assistant helps you decide what to work on when you need it, using facts the server computes and what it has learned about how your work actually goes.

**Status:** scaffold and schema only. The server starts and serves one tool (`get_guide`). The task tools are specified in [`docs/SPEC.md`](docs/SPEC.md) and not yet built.

## Documents

| File | What it is |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | The feature specification: every operation, phase by phase, with acceptance tests. |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | The design and the reasoning behind it. |
| [`INITIAL_PLAN.md`](INITIAL_PLAN.md) | The original idea. |
| [`CLAUDE.md`](CLAUDE.md) | Working notes for whoever implements it. |

## Quick start

Requires Python 3.12 or later and [`uv`](https://docs.astral.sh/uv/).

```
uv sync
uv run pytest
uv run task-orchard info
```

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `TASK_ORCHARD_DB` | Path to the SQLite database file. | `~/.local/share/task-orchard/orchard.db` |
| `TASK_ORCHARD_TZ` | Your timezone as an IANA name, such as `Europe/London`. | The system timezone, else UTC. |

The database is created and migrated automatically on first start.

## Connecting an MCP client

Task Orchard runs as a stdio MCP server. In your client, add a server whose command is:

```
uv run --directory /path/to/this/repo task-orchard
```

and set `TASK_ORCHARD_TZ` in that server's environment if your system timezone is not the one you want.

## Trying it with demo data

```
TASK_ORCHARD_DB=/tmp/orchard-demo.db uv run task-orchard seed-demo
```

This fills a new database with a small realistic set of tasks (projects, blockers, an inbox, scheduled and flexible recurring tasks, history). Point your MCP client at the same `TASK_ORCHARD_DB` to explore it. The contents are described at the top of `src/task_orchard/devtools/seed.py`.
