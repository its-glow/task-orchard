# Task Orchard

A task and time management service for one person, with its own AI assistant.

You cultivate trees of tasks: big vague efforts, one-line reminders, recurring chores, things you want to do more of. In return the assistant helps you decide what to work on when you need it, using facts the server computes and what it has learned about how your work actually goes.

**Status:** phases 0–2A of [`docs/SPEC.md`](docs/SPEC.md) are built. You can talk to Task Orchard's own assistant in the terminal, or reach the same tools from another assistant over MCP. You can capture, create, organise, link and search tasks, ask for a shortlist of what to do now or for what is urgent, and keep a profile. Completing, snoozing, dropping and breaking tasks down arrive in phase 3, and recurrence in phase 4 (see [What works yet](#what-works-yet)).

## How you use it

There are three ways in, over the same service:

| Way in | For | State |
|---|---|---|
| **The app**: `task-orchard chat` | Everyday use. Task Orchard's own assistant, with its guides, your charter and your profile always in front of the model. | Built |
| **Another assistant**, over MCP | Reaching your tasks from an assistant you are already working in, or one that also has your calendar. It gets the same tools, but may not follow the guides. | Built |
| **REST** | Programs and, later, a phone app. | Phase 6 |

## Documents

| File | What it is |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | The feature specification: every operation, phase by phase, with acceptance tests. |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | The design rationale: why it is built the way it is. |
| [`INITIAL_PLAN.md`](INITIAL_PLAN.md) | The original idea. |
| [`CLAUDE.md`](CLAUDE.md) | Working notes for whoever implements it. |

## Quick start

Requires Python 3.12 or later and [`uv`](https://docs.astral.sh/uv/). The assistant calls the Claude API, so it also needs credentials: `ANTHROPIC_API_KEY` in the environment, or a login made with `ant auth login`. Model calls are billed to that account; Task Orchard never stores the key.

```
uv sync
uv run pytest                 # about 45 seconds; never calls a model
TASK_ORCHARD_DB=/tmp/orchard-demo.db uv run task-orchard seed-demo
TASK_ORCHARD_DB=/tmp/orchard-demo.db uv run task-orchard chat
```

Leave out `TASK_ORCHARD_DB` to use your real database at the default path.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `TASK_ORCHARD_DB` | Path to the SQLite database file. | `$XDG_DATA_HOME/task-orchard/orchard.db`, which is normally `~/.local/share/task-orchard/orchard.db` |
| `TASK_ORCHARD_TZ` | Your timezone as an IANA name, such as `Europe/London`. | `$TZ`, then the system timezone, else UTC. |
| `TASK_ORCHARD_MODEL_BASIC` | The model for everyday conversation with the assistant. | `claude-haiku-5-5` |
| `TASK_ORCHARD_MODEL_STRATEGIC` | The model for work that needs more thought, chosen with `chat --strategic` or `/strategic`. | `claude-haiku-5-5` |

The database is created and migrated automatically on first start. Times in results are shown in this timezone; if none can be detected, UTC is assumed and the assistant is told to ask you to set it. `task-orchard info` prints what is in use.

## Commands

```
task-orchard chat         talk to Task Orchard's assistant in the terminal
task-orchard ask "..."    one question, one answer, then exit
task-orchard mcp          the MCP server over stdio (what an MCP client launches)
task-orchard              the same as mcp; so is `serve`, until phase 6
task-orchard info         print the database, timezone and models in use
task-orchard init         create or migrate the database, then exit
task-orchard seed-demo    fill an EMPTY database with demo data
```

Run them with `uv run task-orchard ...` from the repository, or `uv run --directory /path/to/repo task-orchard ...` from anywhere.

## Talking to the assistant

`task-orchard chat` is a conversation in the terminal. Each line you type is one turn. Answers appear on stdout; each tool call the assistant makes is shown on stderr as it happens, such as `-> get_candidates(minutes_available=30, energy="low")`, with ` !! <error>` after it if the call failed.

| In chat | Does |
|---|---|
| `/strategic`, `/basic` | Switch between the two model tiers for the turns that follow. The conversation carries over. A session starts on basic, or on strategic with `chat --strategic`. |
| `/new` | Start a fresh conversation, which reads your profile again. |
| `/quit` | Leave. End of input (Ctrl-D) does the same. |

`chat --transcript FILE` appends every message and tool result to FILE as JSON lines. `ask "..."` runs one turn and exits: 0 on success, 1 if the model could not be reached.

The two tiers differ in model (both Haiku by default) and in how much effort the model spends: medium for basic, high for strategic. Each conversation re-sends what came before; the prompt and the conversation are marked for caching to keep that cheap. When a conversation gets very long you are told, and `/new` starts afresh without losing anything, because your tasks and profile live in the database.

## From another assistant (MCP)

Task Orchard is also a stdio MCP server, so an assistant you already use can read and change your tasks. Most clients take a JSON entry like this (Claude Desktop's `claude_desktop_config.json`, Cursor, and others):

```json
{
  "mcpServers": {
    "task-orchard": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/this/repo", "task-orchard", "mcp"],
      "env": {
        "TASK_ORCHARD_DB": "/tmp/orchard-demo.db",
        "TASK_ORCHARD_TZ": "Europe/London"
      }
    }
  }
}
```

Other MCP hosts take the same command (`uv run --directory /path/to/this/repo task-orchard mcp`) and environment variables through their own way of adding a command-line server. Configurations that leave out `mcp` keep working.

Notes:

- **GUI clients often do not see your shell's `PATH`.** If the server fails to start, use the full path to `uv` as the command (`which uv` prints it).
- No Claude API credentials are needed here: the outside assistant brings its own model.
- The server tells the client to call `read_profile` and read the `overview` guide at the start of each conversation. The guides are also offered as MCP prompts, and through the `get_guide` tool for clients that do not show prompts. Whether a client does any of this is up to the client, which is why the app exists: there, the guides and the profile are always in the prompt.
- The app and MCP clients can use the same database at the same time.
- **Testing one branch while another is being developed:** check the branch out in its own worktree and point the client there, so switching branches in the main checkout never changes the server under you.

  ```
  git worktree add ../task-orchard-stable <branch>
  # then use --directory /path/to/task-orchard-stable
  ```

## Trying it with demo data

```
TASK_ORCHARD_DB=/tmp/orchard-demo.db uv run task-orchard seed-demo
```

This fills a new database with a small, realistic set of tasks: projects with blockers, a release due in 20 hours, a wanted side project, an inbox of three captures, scheduled and flexible recurring tasks, past completions, and a profile with a charter. The contents are described at the top of [`src/task_orchard/devtools/seed.py`](src/task_orchard/devtools/seed.py). Point `chat` or your MCP client at the same `TASK_ORCHARD_DB`.

Dates in the demo are relative to when it was seeded. To start again, delete the database and its two companion files, then seed again:

```
rm /tmp/orchard-demo.db /tmp/orchard-demo.db-wal /tmp/orchard-demo.db-shm
```

### A shakedown

With the demo database, start `task-orchard chat`, try these, and watch the tool calls on stderr. An MCP client works too, but should also call `read_profile` first, since its prompt does not hold your profile.

| Say | Expect |
|---|---|
| "What should I do now?" | `get_candidates`; two to five options with reasons. The release work should come first, and the game should be among them (the charter asks for time on it). |
| "Is anything urgent?" | `get_critical`: the release steps, the overdue weekly summary, and the certificate renewal, which is low priority on its own but blocks the release. |
| "I've got 30 minutes and I'm tired." | `get_candidates` with `minutes_available` and `energy`: nothing estimated over 30 minutes (the flaky test drops out), quick low-drain things like the certificate and the milk instead. |
| "What's next on the release?" | `search_tasks` to find it, then `get_candidates` with `under_id`: only the flaky test is free; the notes and the publish wait on it. |
| "What's next on the offsite?" | A harder one: nothing under it is free, because the whole offsite waits on the budget approval. A good answer says so and offers the budget task. |
| "Triage my inbox." | The `triage` guide: guesses for each capture, at most one question per task asked in one batch, then a two- or three-line summary of what was assumed. |
| "Remind me to buy stamps, call Mum and fix the bike light." | `capture_tasks` with three items, no questions. |
| "The bike light is for Saturday's ride." | `update_task` with a deadline, recorded as yours (`sources: {"deadline": "user"}`). |
| "Show me my projects." | `get_tree`. |
| "Remember I'm not a morning person." | `write_profile_note`. |

Things worth noticing: whether the assistant uses relative times ("in 2 days") rather than working out dates, whether it follows the guides when triaging or planning, and whether error messages lead it to a correct retry. Try the harder rows on both tiers.

Two of these rows also run as automated scenarios against a real model: `uv run pytest -m scenario -s`. They are left out of the normal test run because they cost money.

## What works yet

| Phase | What | State |
|---|---|---|
| 1 | `capture_tasks`, `create_task`, `update_task`, `link_tasks`, `unlink_tasks`, `get_task`, `get_tree`, `search_tasks` | Built |
| 2 | `get_candidates`, `get_critical`, `read_profile`, `write_profile_note`; guides `triage`, `what-now` | Built |
| 2A | The app: `task-orchard chat` and `ask`, two model tiers, one shared tool catalogue, model scenarios | Built |
| 3 | `complete_task`, `snooze_task`, `drop_tasks`, `reopen_task`, `start_task`, `subdivide_task`, patterns | Not yet |
| 4 | Recurrence: creating recurring tasks, spawning occurrences | Not yet |
| 5 | Review queue, activity, merging, `set_timezone`, onboarding and review guides | Not yet |
| 6 | One long-running service: REST, MCP over HTTP, unattended runs (housekeeping, morning and evening) | Not yet |

Until phase 3 you cannot finish or put off a task, so a shakedown is about capturing, organising and choosing. Some error hints mention `reopen_task`, which arrives in phase 3. Recurring tasks in the demo can be read but not changed yet.
