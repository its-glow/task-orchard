# Task Orchard

A task and time management service for one person, with its own AI assistant.

You cultivate trees of tasks: big vague efforts, one-line reminders, recurring chores, things you want to do more of. In return the assistant helps you decide what to work on when you need it, using facts the server computes and what it has learned about how your work actually goes.

**Status:** phases 0–2 of [`docs/SPEC.md`](docs/SPEC.md) are built. You can capture, create, organise, link and search tasks, ask for a shortlist of what to do now or for what is urgent, and keep a profile. Next is phase 2A, Task Orchard's own assistant in the terminal. Completing, snoozing, dropping and breaking tasks down arrive in phase 3, and recurrence in phase 4 (see [What works yet](#what-works-yet)).

## How you use it

There are three ways in, over the same service:

| Way in | For | State |
|---|---|---|
| **The app**: `task-orchard chat` | Everyday use. Task Orchard's own assistant, with its guides, your charter and your profile always in front of the model. | Phase 2A, not built yet |
| **Another assistant**, over MCP | Reaching your tasks from an assistant you are already working in, or one that also has your calendar. It gets the same tools, but may not follow the guides. | Works today for phases 1–2 |
| **REST** | Programs and, later, a phone app. | Phase 6 |

**Until phase 2A is built, an MCP client is the only way to talk to Task Orchard.** Set one up as described under [From another assistant](#from-another-assistant-mcp).

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
uv run pytest                 # about 30 seconds
uv run task-orchard info      # which database and timezone will be used
```

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `TASK_ORCHARD_DB` | Path to the SQLite database file. | `~/.local/share/task-orchard/orchard.db` |
| `TASK_ORCHARD_TZ` | Your timezone as an IANA name, such as `Europe/London`. | `$TZ`, then the system timezone, else UTC. |
| `TASK_ORCHARD_MODEL_BASIC` | The model for everyday conversation with the app. *(From phase 2A.)* | `claude-haiku-5-5` |
| `TASK_ORCHARD_MODEL_STRATEGIC` | The model for work that needs more thought, chosen with `chat --strategic` or `/strategic`. *(From phase 2A.)* | `claude-haiku-5-5` |

The app calls the Claude API, so from phase 2A it also needs credentials for that: `ANTHROPIC_API_KEY` in the environment, or a login made with the `ant` CLI. Task Orchard never stores the key. Model calls are billed to that account. Using Task Orchard from another assistant over MCP needs none of this.

The database is created and migrated automatically on first start. Times in results are shown in this timezone; if none can be detected, UTC is assumed and the assistant is told to ask you to set it.

## Commands

```
task-orchard              run the MCP server over stdio (what an MCP client launches)
task-orchard info         print the database path and timezone in use
task-orchard init         create or migrate the database, then exit
task-orchard seed-demo    fill an EMPTY database with demo data
```

Arriving with phase 2A:

```
task-orchard chat         talk to Task Orchard's assistant in the terminal
task-orchard ask "..."    one question, one answer, then exit
task-orchard mcp          the MCP server over stdio, by its own name
```

Run them with `uv run task-orchard ...` from the repository, or `uv run --directory /path/to/repo task-orchard ...` from anywhere.

## From another assistant (MCP)

Task Orchard is also a stdio MCP server, so an assistant you already use can read and change your tasks. Most clients take a JSON entry like this (Claude Desktop's `claude_desktop_config.json`, Cursor, and others):

```json
{
  "mcpServers": {
    "task-orchard": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/this/repo", "task-orchard"],
      "env": {
        "TASK_ORCHARD_DB": "/tmp/orchard-demo.db",
        "TASK_ORCHARD_TZ": "Europe/London"
      }
    }
  }
}
```

In Goose, run `goose configure`, choose *Add Extension*, then *Command-line Extension*, and give the same command (`uv run --directory /path/to/this/repo task-orchard`) and environment variables.

Notes:

- **GUI clients often do not see your shell's `PATH`.** If the server fails to start, use the full path to `uv` as the command (`which uv` prints it).
- Leave out `TASK_ORCHARD_DB` to use your real database at the default path. Use a separate file for experiments.
- The server tells the client to call `read_profile` and read the `overview` guide at the start of each conversation. The guides are also offered as MCP prompts, and through the `get_guide` tool for clients that do not show prompts. Whether a client does any of this is up to the client, which is why the app exists: there, the guides and the profile are always in the prompt.
- **Testing one branch while another is being developed:** check the branch out in its own worktree and point the client there, so switching branches in the main checkout never changes the server under you.

  ```
  git worktree add ../garden-phase2 phase-2
  # then use --directory /path/to/garden-phase2
  ```

## Trying it with demo data

```
TASK_ORCHARD_DB=/tmp/orchard-demo.db uv run task-orchard seed-demo
```

This fills a new database with a small, realistic set of tasks: projects with blockers, a release due in 20 hours, a wanted side project, an inbox of three captures, scheduled and flexible recurring tasks, past completions, and a profile with a charter. The contents are described at the top of [`src/task_orchard/devtools/seed.py`](src/task_orchard/devtools/seed.py). Point your client at the same `TASK_ORCHARD_DB`.

Dates in the demo are relative to when it was seeded. To start again, delete the database and its two companion files, then seed again:

```
rm /tmp/orchard-demo.db /tmp/orchard-demo.db-wal /tmp/orchard-demo.db-shm
```

### A shakedown

With the demo database connected, try these in a new conversation and watch which tools the assistant calls. Today that means an MCP client; from phase 2A, `task-orchard chat`, which prints each tool call as it happens and never needs the `read_profile` call in the first row.

| Say | Expect |
|---|---|
| "What should I do now?" | `read_profile`, then `get_candidates`; two to five options with reasons. The release work should come first, and the game should be among them (the charter asks for time on it). |
| "Is anything urgent?" | `get_critical`: the release steps, the overdue weekly summary, and the certificate renewal, which is low priority on its own but blocks the release. |
| "I've got 30 minutes and I'm tired." | `get_candidates` with `minutes_available` and `energy`: nothing estimated over 30 minutes (the flaky test drops out), quick low-drain things like the certificate and the milk instead. |
| "What's next on the release?" | `search_tasks` to find it, then `get_candidates` with `under_id`: only the flaky test is free; the notes and the publish wait on it. |
| "What's next on the offsite?" | A harder one: nothing under it is free, because the whole offsite waits on the budget approval. A good answer says so and offers the budget task. |
| "Triage my inbox." | The `triage` guide: guesses for each capture, at most one question per task asked in one batch, then a two- or three-line summary of what was assumed. |
| "Remind me to buy stamps, call Mum and fix the bike light." | `capture_tasks` with three items, no questions. |
| "The bike light is for Saturday's ride." | `update_task` with a deadline, recorded as yours (`sources: {"deadline": "user"}`). |
| "Show me my projects." | `get_tree`. |
| "Remember I'm not a morning person." | `write_profile_note`. |

Things worth noticing: whether the assistant uses relative times ("in 2 days") rather than working out dates, whether it reads the guides before triaging or planning, and whether error messages lead it to a correct retry.

## What works yet

| Phase | What | State |
|---|---|---|
| 1 | `capture_tasks`, `create_task`, `update_task`, `link_tasks`, `unlink_tasks`, `get_task`, `get_tree`, `search_tasks` | Built |
| 2 | `get_candidates`, `get_critical`, `read_profile`, `write_profile_note`; guides `triage`, `what-now` | Built |
| 2A | The app: `task-orchard chat` and `ask`, one shared tool catalogue, model scenarios | Next |
| 3 | `complete_task`, `snooze_task`, `drop_tasks`, `reopen_task`, `start_task`, `subdivide_task`, patterns | Not yet |
| 4 | Recurrence: creating recurring tasks, spawning occurrences | Not yet |
| 5 | Review queue, activity, merging, `set_timezone`, onboarding and review guides | Not yet |
| 6 | One long-running service: REST, MCP over HTTP, unattended runs (housekeeping, morning and evening) | Not yet |

Until phase 3 you cannot finish or put off a task, so a shakedown is about capturing, organising and choosing. Some error hints mention `reopen_task`, which arrives in phase 3. Recurring tasks in the demo can be read but not changed yet.
