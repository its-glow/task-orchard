# Task Orchard

A single-user task and time management service with its own AI assistant. The server (`core` and the database) stores tasks and computes facts; a model makes the judgements. That model is reached through **the app**, Task Orchard's own loop (`agent/`, Phase 2A), which is the primary way in. MCP (for other assistants) and REST (for programs) are adapters over the same operations and are built out in Phase 6.

## Documents, in order of authority

1. `docs/SPEC.md` — the build document. What every operation does, with acceptance tests per phase. **Follow it.**
2. `IMPLEMENTATION_PLAN.md` — the design rationale: why the design is what it is. Read once for context.
3. `INITIAL_PLAN.md` — the original idea. Historical.

Phases 0 (scaffold and schema), 1 (tasks and graph), 2 (prioritisation), 2A (the app), 3 (leading edge and completion), 4 (recurrence) and 5 (learning and review) are done. Next is Phase 6. Work proceeds one phase at a time from `docs/SPEC.md` section 11. SPEC sections 10.1 to 10.3 describe the catalogue, the app and the MCP adapter.

The MCP adapter in `mcp/` is generated from the catalogue and must keep working, but it is not where behaviour is designed or tested. Do no MCP-specific work before Phase 6.

## Commands

```
uv sync                      # install
uv run pytest                # all tests but scenarios (about two minutes)
uv run pytest -m scenario -s  # real-model scenarios: cost money, ask first
uv run pytest tests/test_x.py -k name
uv run ruff check src tests  # lint
uv run ruff format src tests # format
uv run task-orchard info     # which database, timezone and models are in use
uv run task-orchard dump     # every task as CSV, derived facts included (--open, --sources)
TASK_ORCHARD_DB=/tmp/demo.db uv run task-orchard seed-demo   # a populated database to try things on
```

## Layout

```
src/task_orchard/
  config.py          settings from the environment
  cli.py             task-orchard command
  tools.py           the tool catalogue: one function per operation, call_tool (done)
  guides.py          loads skills (done)
  core/              all logic; imports nothing from tools, agent/, mcp/ or api/
    enums.py           every enumerated value (done)
    errors.py          OrchardError and friends (done)
    clock.py           the only source of "now" (done)
    events.py          record(): the only way to write an event (done)
    service.py         Orchard: one method per operation (Phases 1-3 done)
    wire.py            reading arguments: lenient names, ids, sources (done)
    timefmt.py         parsing and formatting times        (done)
    views.py           JSON row and detail builders        (done)
    graph.py           blocking graph and cycle checks     (done)
    derive.py          Snapshot: predicates, effective values, at risk (done)
    shortlist.py       get_candidates, get_critical; the 5.5 constants (done)
    history.py         counts, digests, the balance        (done)
    review.py          review queue, recent activity       (done)
    dump.py            every task as flat rows, for `task-orchard dump`
    recurrence.py      roll_forward and friends            (done)
  db/
    models.py          the schema; source of truth (done)
    types.py           UTCDateTime, IntEnumType (done)
    engine.py          engine, sessions, init_db (done)
    migrations/        Alembic
  agent/             the app: Task Orchard's own assistant (done)
    model.py           Model protocol, AnthropicModel, tiers; the only importer of anthropic
    preamble.md        the fixed opening of the system prompt
    prompt.py          builds the system prompt: preamble, guides, profile; the [Now:] line
    loop.py            Session, start_session, run_turn: the tool-use loop; MAX_STEPS
    chat.py            task-orchard chat and ask
    commands.py        chat's tester commands: /tools, /tool, /last, /prompt, /usage
  mcp/
    server.py          FastMCP adapter: INSTRUCTIONS, get_guide, prompts, and every
                       catalogue tool registered in a loop (done)
  api/               REST routes (Phase 6)
  skills/              one markdown file per skill: overview, triage, what-now,
                       subdivide, complete-and-reflect, onboarding,
                       plan-time-block, housekeeping, weekly-review
  devtools/seed.py     demo data, addressed by handle
tests/
  conftest.py          fixtures: orchard, clock, seeded, session
  scripted.py          ScriptedModel and reply helpers for testing the app
  scenarios/           real-model checks, run with `pytest -m scenario` (harness.py)
```

## Rules

- **Facts in the server, judgement in the agent, by default.** No LLM calls and no opaque weighted scoring. Simple server-side filtering and ordering is fine when it decides what to show, is stated in the spec, reports what it left out, and can be adjusted by a parameter. See SPEC section 1 for the test, and add anything new to the spec before building it.
- **Derived state is never stored.** Blocked, dormant, candidate, effective priority, deadline and motive, and all statistics are computed on read. Do not add columns for them.
- **Every mutation goes through `Orchard`**, inside `with self.tx() as tx:`, and writes its events with `record()` in that same transaction. SPEC section 6.4 lists exactly which events each operation writes.
- **Time comes from `tx.now`.** Never call `datetime.now()`. Datetimes are aware UTC everywhere except `Task.recur_dtstart`.
- **Tools are thin and defined once.** A tool takes arguments, calls one `Orchard` method and returns its dict. It lives in `tools.py`, and the app and the MCP adapter are both built from that catalogue. No logic in `tools.py`, `agent/`, `mcp/` or `api/`.
- **The app judges nothing in code.** `agent/` builds the prompt, sends the conversation and runs the tools the model asks for. Anything that looks like task logic belongs in `core`; anything that looks like judgement belongs in a guide.
- **A session is append-only.** The app never edits, re-orders or drops a system prompt, tool list or message it has already sent (SPEC 10.2). Do not put the time or anything else that varies in the system prompt.
- **Tests never call a model.** Use `ScriptedModel`. Scenarios are the one exception: they are marked `scenario`, excluded from the normal run, and cost money, so ask before running them.
- **Errors are for the model to read.** Raise `OrchardError` subclasses with a `hint` that says what to do next.
- **Responses are bounded.** Respect the limits in the spec; omit empty keys.
- **Tests first.** Each phase lists its test modules and what they must prove. Write those, then the code. Use the `orchard` and `clock` fixtures; move the clock, never sleep.

## Changing the schema

1. Edit `src/task_orchard/db/models.py`.
2. `TASK_ORCHARD_DB=/tmp/dev.db uv run alembic upgrade head`
3. `TASK_ORCHARD_DB=/tmp/dev.db uv run alembic revision --autogenerate -m "what changed"`
4. Review the generated file. Autogenerate does **not** see changes to CHECK constraints or to the full-text tables; write those by hand.
5. `uv run pytest tests/test_schema.py` — fails if models and migrations disagree.

Never edit a migration once a real database has been created from it; add a new one. Real databases now exist (the user runs an MCP host from Phase 1), so `0001_initial_schema.py` is frozen: every schema change from here on is a new revision. Migrations must not import application types (the Alembic environment renders ours as plain SQLAlchemy types).

## Things that will bite

- **stdout belongs to MCP** when the server runs over stdio. Print diagnostics to stderr.
- **FastMCP is at 4.x** here and changes quickly. Check its current documentation before using an API you remember from an older version. `tests/test_scaffold.py` shows the in-memory client pattern that works with the installed version.
- **The Claude API changes quickly too.** Model ids, how thinking and effort are set, what a refusal looks like and how caching is marked have all changed recently. Read the current documentation (or the `claude-api` skill) before changing `agent/model.py`; do not write the request from memory.
- **JSON has no "omitted" versus "null"** that clients agree on. That is why `update_task` unsets fields through an explicit `clear` list.
- **A CHECK that compares with NULL passes.** Use `coalesce()` as `models.py` does.
- **Never pass user text straight to an FTS `MATCH`.** Quote each word (SPEC 8.12).
- The seed writes rows directly because it predates the service. Tests refer to seeded tasks by handle (`seeded["cert"]`); keep the handles' meanings if you rewrite it.

## When the spec is silent or looks wrong

Do not guess silently. Choose the reading that best fits SPEC section 1, implement it, state the rule in the section of the spec it belongs to, and add a row to SPEC section 13 saying what you decided and why. If the question changes behaviour the user would notice, stop and ask instead.
