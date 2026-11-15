# Task Orchard

A single-user task and time management service with an MCP interface. The server stores tasks and computes facts; the model in the MCP client makes the judgements.

## Documents, in order of authority

1. `docs/SPEC.md` — the build document. What every operation does, with acceptance tests per phase. **Follow it.**
2. `IMPLEMENTATION_PLAN.md` — why the design is what it is. Read once for context.
3. `INITIAL_PLAN.md` — the original idea. Historical.

Phase 0 (scaffold and schema) is done. Work proceeds one phase at a time from `docs/SPEC.md` section 11.

## Commands

```
uv sync                      # install
uv run pytest                # all tests (about 7 seconds)
uv run pytest tests/test_x.py -k name
uv run ruff check src tests  # lint
uv run ruff format src tests # format
uv run task-orchard info     # which database and timezone are in use
TASK_ORCHARD_DB=/tmp/demo.db uv run task-orchard seed-demo   # a populated database to try things on
```

## Layout

```
src/task_orchard/
  config.py          settings from the environment
  cli.py             task-orchard command
  core/              all logic; imports nothing from mcp/ or api/
    enums.py           every enumerated value (done)
    errors.py          OrchardError and friends (done)
    clock.py           the only source of "now" (done)
    events.py          record(): the only way to write an event (done)
    service.py         Orchard: transaction wrapper done; operations to add
    timefmt.py         parsing and formatting times        (Phase 1)
    views.py           JSON row and detail builders        (Phase 1)
    graph.py           blocking graph and cycle checks     (Phase 1)
    derive.py          predicates, effective values        (Phases 1-2)
    shortlist.py       get_candidates, get_critical        (Phase 2)
    history.py         counts, digests, balance, review    (Phases 3, 5)
    recurrence.py      roll_forward and friends            (Phase 4)
  db/
    models.py          the schema; source of truth (done)
    types.py           UTCDateTime, IntEnumType (done)
    engine.py          engine, sessions, init_db (done)
    migrations/        Alembic
  mcp/
    server.py          FastMCP adapter; get_guide done, tools to add
    guides.py          loads skills (done)
  skills/              one markdown file per skill; overview.md exists
  devtools/seed.py     demo data, addressed by handle
tests/
  conftest.py          fixtures: orchard, clock, seeded, session
```

## Rules

- **Facts in the server, judgement in the agent, by default.** No LLM calls and no opaque weighted scoring. Simple server-side filtering and ordering is fine when it decides what to show, is stated in the spec, reports what it left out, and can be adjusted by a parameter. See SPEC section 1 for the test, and add anything new to the spec before building it.
- **Derived state is never stored.** Blocked, dormant, candidate, effective priority, deadline and motive, and all statistics are computed on read. Do not add columns for them.
- **Every mutation goes through `Orchard`**, inside `with self.tx() as tx:`, and writes its events with `record()` in that same transaction. SPEC section 6.4 lists exactly which events each operation writes.
- **Time comes from `tx.now`.** Never call `datetime.now()`. Datetimes are aware UTC everywhere except `Task.recur_dtstart`.
- **Tools are thin.** An MCP tool takes arguments, calls one `Orchard` method, returns its dict, wrapped in `guarded`. No logic in `mcp/`.
- **Errors are for the model to read.** Raise `OrchardError` subclasses with a `hint` that says what to do next.
- **Responses are bounded.** Respect the limits in the spec; omit empty keys.
- **Tests first.** Each phase lists its test modules and what they must prove. Write those, then the code. Use the `orchard` and `clock` fixtures; move the clock, never sleep.

## Changing the schema

1. Edit `src/task_orchard/db/models.py`.
2. `TASK_ORCHARD_DB=/tmp/dev.db uv run alembic upgrade head`
3. `TASK_ORCHARD_DB=/tmp/dev.db uv run alembic revision --autogenerate -m "what changed"`
4. Review the generated file. Autogenerate does **not** see changes to CHECK constraints or to the full-text tables; write those by hand.
5. `uv run pytest tests/test_schema.py` — fails if models and migrations disagree.

Never edit a migration that has been released; add a new one. Migrations must not import application types (the Alembic environment renders ours as plain SQLAlchemy types).

## Things that will bite

- **stdout belongs to MCP** when the server runs over stdio. Print diagnostics to stderr.
- **FastMCP is at 4.x** here and changes quickly. Check its current documentation before using an API you remember from an older version. `tests/test_scaffold.py` shows the in-memory client pattern that works with the installed version.
- **JSON has no "omitted" versus "null"** that clients agree on. That is why `update_task` unsets fields through an explicit `clear` list.
- **A CHECK that compares with NULL passes.** Use `coalesce()` as `models.py` does.
- **Never pass user text straight to an FTS `MATCH`.** Quote each word (SPEC 8.12).
- The seed writes rows directly because it predates the service. Tests refer to seeded tasks by handle (`seeded["cert"]`); keep the handles' meanings if you rewrite it.

## When the spec is silent or looks wrong

Do not guess silently. Choose the reading that best fits SPEC section 1, implement it, and add a row to SPEC section 13 saying what you decided and why. If the question changes behaviour the user would notice, stop and ask instead.
