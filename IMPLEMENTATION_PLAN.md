# Task Orchard — Implementation Plan

Status: draft 4, 2026-11-14. Built from `INITIAL_PLAN.md` and five rounds of review.

**Revised 2026-11-15: the app comes first.** The plan below was written with an MCP client as the way in. After Phases 1 and 2 were in use, that changed: Task Orchard gets its own assistant (the app), and MCP and REST become adapters built later. Section 12 gives the reasoning. Sections 1, 3, 6, 7, 9 and 10 have been brought into line; the task model in sections 4 and 5 is untouched.

**This plan is now the design rationale.** The build document is `docs/SPEC.md`, which specifies every operation and is authoritative where the two differ; its section 13 lists what it refined. Phase 0 (scaffold and schema) is built. Your inline comments are folded into the sections they affect. Items marked **(default)** are my choices where nothing was said; section 11 lists what is still open.

## 1. Decisions

| Topic | Decision |
|---|---|
| Where the AI lives | In a loop above the server: Task Orchard's own (the app), or an outside assistant's. The server itself never calls an LLM. (Revised; originally "in the MCP client or agent".) |
| Hosting | One machine, SQLite file. HTTP access is added later without a rewrite. |
| First client | The app: `task-orchard chat`, a terminal conversation with our own prompt, guides and choice of model. (Revised; originally a chat-style MCP client, with Goose as the working assumption.) |
| MCP and REST | Adapters over the same operations as the app. MCP is for reaching tasks from another assistant; REST is for programs. Both are built out in Phase 6. |
| "Not live yet" | One field, `snooze_until`, replaces both "care about date" and "snooze period". |
| Own vs. effective values | A task stores its own priority and deadline. Effective priority and deadline are derived on every read and never stored, so removing a link reverts them immediately (section 5.3). |
| Recurrence | Exactly one deterministic pattern (a calendar schedule). Every other kind of repetition is flexible and agent-guided (section 5.5). |
| Missed scheduled instances | They stay open and overdue until completed or explicitly dropped. At most two instances of one scheduled task are live; when a third would spawn, the oldest is closed as `missed`. |
| Server vs. agent | The server owns invariants, arithmetic and retrieval. The agent owns judgement, including ranking (section 2). Routine work is expected to run on a small model such as Haiku. |
| History | Raw completions and events are kept, but the AI reads compact **patterns**, never raw rows (section 5.9). Confirmed as matching the intent. |
| Flexible recurrence | `gap_days` as a fallback snooze and completing a flexible task directly are both accepted. |
| Firmness | `hard`, `soft`, or no deadline. A dated wish is a soft deadline with low priority. |
| Upkeep | A housekeeping skill tidies overdue soft deadlines, duplicates and untouched tasks. |
| Memory | Lives on the server: patterns for kinds of task, profile notes for the user in general. |
| Task demands | Two axes, `activation` and `drain`: how hard it is to start, and how draining it is to do (section 5.4). Accepted for now; may be revisited after real use. |
| Filling in fields | Only a title is ever required. The agent guesses the rest, infers from patterns, and asks only where an answer is both unclear and consequential (section 5.11). |
| Want vs. have-to | A third marker, `motive`, separate from activation. The server stores it and counts the balance of time spent; what to do about the balance is policy, held in the charter (sections 5.4 and 5.9). |
| Deadlines | Optional. No deadline is the normal state for wish-level tasks. |
| Parent close | The agent asks, but acts on its own when the answer is obvious. |
| Durations | Self-reported at completion. Starts are logged separately as a resistance signal. |
| Directiveness | The server exposes query primitives; the agent composes them. Time-block planning is a skill. |
| Calendar integration | Out of scope. Later, give the same agent a calendar MCP alongside this one. |
| Priority scale | 1–4 with names, revisable. |

## 2. Design principles

1. **The server computes facts; the agent makes judgements.** The server owns three things:
   - *invariants*: what must hold whatever the model does (no cycles, atomic changes, the two-instance cap, the event log);
   - *arithmetic and the clock*: date maths, schedules, derived values, aggregation;
   - *retrieval*: cutting hundreds of tasks down to what fits in context.

   The agent owns what things mean: priority, breakdown, ranking, when to repeat, whether a parent is done, what to learn. When a new behaviour is proposed, the test is whether it is a fact or a judgement. This is a default, not a prohibition: simple, visible, adjustable filtering in the server is fine, and logic can move there when real use shows the agent is handed too much. What stays out is opaque weighted scoring.
2. **Built for a small model.** Because routine work may run on Haiku, facts arrive ready to use ("due in 2 days", never a timestamp to subtract), tools validate their input and return corrective errors, multi-step changes are single atomic calls, and skills are explicit about steps.
3. **Derived, not stored.** Blocked, dormant, effective priority, effective deadline and pattern statistics are all computed on read. Nothing can go stale, and undoing a change needs no cleanup.
4. **Context is a budget.** Every tool response is bounded. Lists return compact rows and details are fetched on request. History reaches the model only as fixed-size digests.
5. **Every mutation is logged.** The event log makes agent mistakes auditable and reversible. This matters more as more is delegated.

## 3. Architecture

```
 the app (our loop + a model)    another assistant       Android app (later)
        │ in-process                  │ MCP                   │ REST
        │                        FastMCP adapter         FastAPI adapter
        └────────────┬────────────────┘                       │
              tool catalogue                                  │
                     └───────────────────┬────────────────────┘
                               core service layer
                (tasks, graph, shortlist, recurrence, history, memory)
                                         │
                                      SQLite
```

- **Core** is a plain Python library with no model, MCP or HTTP imports. Everything above it is a thin wrapper.
- **The tool catalogue** defines each tool once. The app and the MCP adapter are both built from it.
- **The app** is a short loop: send the conversation and the tools to a model, run what it asks for against core, repeat until it answers. It is where model calls happen, and it makes no judgement in code.
- **No background process.** A `roll_forward(now)` step runs at the start of every request and handles everything time-driven (section 5.5). Unattended work in Phase 6 is a command the operating system's scheduler runs, not a daemon of ours.
- **Time** is stored in UTC. The user's timezone is config. A date-only deadline means end of that day locally. Schedules are computed in local time so that Friday stays Friday across daylight-saving changes.
- **Stack (default):** Python 3.12+, `uv`, SQLAlchemy 2 with Alembic migrations, Pydantic models at the boundary, SQLite FTS5 for search, the `anthropic` SDK for the app's model calls, FastMCP for the MCP adapter, `pytest` with `hypothesis`. FastMCP and the Claude API both move quickly, so check their current docs before using either.

```
src/task_orchard/
  core/      graph, derived state, shortlist, recurrence, history, service
  db/        schema, migrations
  tools.py   the tool catalogue (Phase 2A)
  agent/     the app: prompt, loop, model, chat (Phase 2A)
  mcp/       FastMCP adapter, prompts
  api/       FastAPI routes (Phase 6)
  skills/    one markdown source per skill
tests/
```

## 4. Data model

This is the outline the schema grew from. The schema as built is `src/task_orchard/db/models.py`.

### `tasks`

| Field | Notes |
|---|---|
| `id`, `title`, `description`, `notes` | |
| `recur_mode` | Empty for a plain task; `scheduled` or `flexible` for a recurring one. A project is a task with children. (Earlier drafts had a separate `kind` field; it duplicated this and was dropped.) |
| `status` | `open`, `done`, `dropped`, `missed` (scheduled instances closed by the cap). |
| `parent_id` | Single parent. Ordering between siblings uses blocker links. |
| `priority` | `low`, `normal`, `high` or `critical`; may be empty, which means inherit. |
| `deadline`, `deadline_firmness` | Both optional. Firmness is `hard` or `soft`. **(default)** The spec's third level is covered by having no deadline. |
| `snooze_until` | Do not surface before this. A snoozed parent hides its subtree; a snoozed recurring task is paused. |
| `achievable` | `unknown`, `yes`, `no`. |
| `estimate_minutes` | |
| `activation`, `drain` | Low, medium or high (section 5.4). |
| `motive` | `want`, `should`, `have_to`, nullable. Null means inherit from the parent (section 5.4). |
| `field_sources` | For each descriptive field, where its value came from: `guess`, `pattern`, `evidence` or `user` (section 5.11). |
| `conditions` | Free-form tags: `good-weather`, `at-home`, `uninterrupted`. |
| `pattern_id` | The pattern this task belongs to, if any. |
| `triaged` | False for inbox captures. |
| `created_at`, `updated_at`, `closed_at` | |
| Recurring, scheduled | `rrule`, its start and due time, lead time, firmness for instances, `last_spawned_slot`. Instances copy the recurring task's own fields. |
| Recurring, flexible | `cadence` (free text), optional `gap_days`. |
| Scheduled instances | `occurrence_at`: the slot this instance stands for. |

### Other tables

- `task_blocks(blocker_id, blocked_id)`
- `completions(task_id, completed_at, actual_minutes, felt_activation, felt_drain, reflection, estimate_at_completion)`
- `events(id, task_id, type, at, payload)`: append-only.
- `patterns(id, name, description, learned, updated_at)`
- `profile_notes(id, topic, text, updated_at)`

## 5. Core behaviour

### 5.1 Candidate pool (no AI)

A task is a candidate when it is open, triaged, not snoozed (nor is any ancestor), has no open children, and has no open blockers (nor does any ancestor). Scheduled recurring parents never appear; their instances do. A flexible recurring task appears itself when it has no open instance.

### 5.2 Blocking and cycles

Two kinds of edge mean "must finish first": child → parent, and blocker → blocked. A blocker on a parent also blocks every descendant. Adding a link, or moving a task to a new parent, runs a depth-first search over this combined graph and rejects the change with the offending path if it would close a loop.

The inherited case matters. If X blocks project P, and X is itself blocked by P's child A, then A waits on X (inherited) and X waits on A. Explicit links alone show no cycle.

### 5.3 Own and effective values

Computed in memory at query time over all open tasks.

- **Effective priority** = the highest of: own priority, parent's effective priority, effective priority of everything it blocks.
- **Effective deadline** = the earliest of: own deadline, parent's effective deadline, and (the effective deadline of each task it blocks, minus that task's remaining estimate). Firmness travels with whichever deadline wins.

Reads return both the own value and the effective one, with where the effective one came from ("due Thursday via *ship release*, which this blocks"). Because nothing is stored, unlinking or re-parenting needs no cleanup: the next read is simply different.

### 5.4 Task demands and user state

Two axes on each task, each low, medium or high:

| Axis | Question | Dentist call | Hobby gamedev |
|---|---|---|---|
| `activation` | How hard is it to start? | high | medium |
| `drain` | How much does doing it take out of you? | low | high |

- Difficulty is not a separate field. It shows up as drain and as the time estimate.
- Focus is not a separate field. "Needs an uninterrupted block" is a condition tag plus the estimate.
- The user's current state is one number, **energy**, matched against drain. **(default)** Activation is not matched against a state. It drives the agent's strategy instead: offer a smaller first step, place the task at the start of a block, pair it after an easy win, or raise it earlier when it has a deadline.
- Both values are first guessed at triage, then corrected by evidence: the felt values in the completion reflection, deferral counts, and false starts (a `start_task` with no completion afterwards).

**Motive: want versus have-to.** This is a third marker, not a low or negative activation. The two vary independently:

| | Easy to start | Hard to start |
|---|---|---|
| **Want** | Reading a novel | Hobby gamedev |
| **Have to** | Replying to a routine email | Calling the dentist |

The top-right cell is the reason to keep them apart. Wanted-but-hard-to-start work is what obligations crowd out, and it needs both protecting and help starting. Folding want into activation would tell the system that gamedev needs no help.

They also differ in kind. Activation is about the moment of starting and is corrected by evidence. Motive is about why the task exists; it is stable and usually belongs to the root of a tree. So it inherits downward like priority: "fix the build pipeline" under "make my game" is tedious and still counts as time on a want. A child can override it.

- Values are `want`, `should` and `have_to`. A `should` is self-imposed: nobody else is waiting on it, but you would hold yourself to it (exercise, flossing, keeping up with other teams' docs). A work project you happen to enjoy is still a `have_to`; the enjoyment goes in its pattern note.
- The agent sets it at triage and you can correct it. Unlike activation and drain there is no evidence loop.
- The server's part is facts only: the effective motive on every row, reserved shortlist places so wants are not cut (section 5.10), and the balance of time spent (section 5.9). How much to protect wants, and when, is policy in the charter.

### 5.5 Recurrence

A recurring task is never completed, only dropped (retired) or snoozed (paused). There are two modes.

#### Scheduled: the one hard pattern

For "every Friday" or "the 1st of each month". The rule is an RFC 5545 `RRULE` string (`FREQ=WEEKLY;BYDAY=FR`) that the agent writes at creation, evaluated with `dateutil.rrule`. Spawning is driven by the clock alone and is independent of completion:

```
roll_forward(now):                       # start of every request, same transaction
  for each open, un-snoozed scheduled task R:
    slot = first rrule slot after R.last_spawned_slot
    while slot − R.lead_time <= now:
      if R already has 2 open instances:
        close the oldest as missed; log a missed event
      create instance(parent=R, occurrence_at=slot, deadline=slot,
                      fields copied from R)
      R.last_spawned_slot = slot
      slot = next rrule slot after slot
```

- It is idempotent because `last_spawned_slot` only moves forward. A new rule starts with `last_spawned_slot = now`.
- **Lockstep holds by construction.** Completing Friday's instance on Wednesday, or on Saturday, changes nothing about when next Friday's instance appears.
- **Missed instances stay, up to a cap of two live.** An instance not done by its slot remains open and overdue next to the newer one. When a third would spawn, the oldest is closed as `missed`. After three weeks away you find two instances, and the history shows the rest as missed. Dropping one yourself requires a reason and is logged as a cancellation, which is distinct from `missed`.
- **Pause** by snoozing the recurring task. On resume, `last_spawned_slot` jumps to now, so the paused slots are skipped and logged as one paused span, not as misses. **(default)**
- Each instance copies the recurring task's own priority, estimate, activation and drain. Firmness is hard by default.

#### Flexible: everything else

For "laundry about every five days", "run twice a week", "read other teams' docs when there's time", or any reason you would repeat something. The rule is free text in `cadence`, read by the agent. The server does only this:

```
on completing an instance of flexible task R, or completing R directly:
  record the completion                         # R itself stays open
  if R.gap_days: R.snooze_until = now + gap_days
  return a recurrence check: cadence text, pattern digest, recent gaps
```

The agent then uses the recurrence check to decide what happens next. It can leave the default snooze, change it, or spawn a concrete instance with its own soft deadline and priority, as in your original design ("laundry, ideally by Thursday").

- A flexible task with no open instance is itself a candidate once it wakes. The agent can offer it as is ("go for a run?"), and completing it directly records an instance in one call. Spawning a child first is optional ceremony, useful when the instance needs its own deadline or breakdown.
- If `gap_days` is set, the candidate row reports how many days past the wake-up it is, and the agent weighs that. If it is not set, the task competes on priority alone, which is the "when there's free time" case.
- `gap_days` is a fallback so that a just-finished chore does not immediately reappear when the agent sets nothing. It is not a schedule.
- Quotas such as "twice a week" are handled here by the agent using the cadence text and recent history, not by a third rule type.

### 5.6 The leading edge

1. `get_candidates` returns the shortlist. A row whose `achievable` is `no` or `unknown` needs breaking down or assessing.
2. The agent follows the subdivide skill. It acts alone on obvious cases and asks you on unclear ones.
3. It calls `subdivide_task` once with all children and their ordering. The call is atomic.
4. It queries again. The skill caps the depth so this cannot spiral.

### 5.7 Completion and the parent check

`complete_task` does all of the following in one transaction: marks the task done, stores the completion record, attaches the pattern, applies the flexible-recurrence step if relevant, and walks upward. A parent left with no open children is returned as a **parent check** with its description and finished children.

The skill tells the agent to close or keep the parent open without asking when the answer is obvious, to say what it did, and to ask otherwise. "Obviously still open" will be the common clear case. Tuning this is later-phase work.

If nothing is decided, nothing is lost: a non-achievable parent with no open children resurfaces as a candidate marked not achievable.

### 5.8 Logging work that wasn't tracked

The agent searches first, then one of three things happens:

- it matches an open task → complete it;
- it matches a recurring task → complete against it, which records an instance;
- it is new → `complete_task` accepts new-task fields and creates and completes in one call.

### 5.9 History and memory

Your objection to keeping every completion was about what the AI reads, not about storage. The plan separates the two.

**Stored underneath (never read raw by the model):**

- `completions`: one row per finished task.
- `events`: one row per change. The service layer has a single `record()` helper, called inside the same transaction as every mutation, so the log cannot drift from the data. Types: created, updated (with before and after), started, snoozed, dropped, missed, reopened, merged, subdivided, linked, unlinked, paused.

**What the model reads:**

- **Patterns** are the merged archive you described. A pattern is a named kind of work: "performance reviews", "exercise", "phone calls to book things". Each recurring task has one automatically. At completion, the agent files the task under an existing pattern or creates one. A pattern digest is fixed-size:
  - computed on read from the rows underneath: times done, dropped, deferred, false starts; typical actual time against estimate; typical felt activation and drain; last done;
  - a short `learned` note the agent maintains ("draining; goes better in the morning; takes about twice the estimate").
- **The charter** is one pinned profile note: your own statement of what Task Orchard is for and how it should treat you. It is the equivalent of a `SOUL.md`, kept on the server so every client reads the same one. This is where policy lives, such as how much of a week should go to wants or whether a want may be offered as a reward after a have-to. Onboarding drafts it with you, and it can start nearly empty and grow.
- **Profile notes** hold what is true of you in general, including corrections to suggestions ("no chores before noon", "low energy after 3pm"). They are capped in total size; housekeeping consolidates them.
- **Recent activity** is a bounded feed from the event log, for evening reflection and weekly review. It includes the **balance**: time spent over the last 7 and 30 days split by want, should and have-to, from the durations reported at completion.

**How it is used:**

| Moment | Memory consulted |
|---|---|
| Session start | The charter and profile notes. The app puts them in its prompt; an MCP client is told by the server's instructions to read them. |
| Presenting options or planning a block | The balance, read against the charter. |
| Triage of a new task | Matching patterns seed the estimate, activation and drain. So "do a performance review" starts out marked draining because the last ones were. |
| Winnowing candidates | Profile notes; pattern digests of the shortlisted tasks. |
| You correct a suggestion | The agent writes a profile note or updates the pattern's `learned` note. |
| Review queue | Event-derived counts per task: times deferred, starts without a finish, days untouched, days past a soft deadline. The server reports the numbers; the agent decides what is worth raising. |
| An agent makes a wrong change | The event log shows what changed, and `reopen_task` or `update_task` undoes it. |

On using an existing MCP memory server: I would keep memory in Task Orchard. Patterns are joined to task rows, which an external store cannot do; there is one file to back up; and every client behaves the same. Running a general-purpose memory server next to it remains possible.

### 5.10 Shortlisting

Draft 2 had a weighted scoring formula here. That was a judgement heuristic in server code, so it is replaced by a shortlist: the server's job is to cut the pool to about 20 rows and attach the facts, and the agent ranks.

- **Order** is deliberately plain and has no tuned weights: pressing hard deadlines first, then anything due soon, then the rest; within each, by effective priority and then deadline. It only decides what makes the cut.
- **Reserved places.** Wants are typically low priority with no deadline, so the plain order alone would never show them. A few places in every shortlist are held for the top `want` candidates. This guarantees the agent sees them; whether to offer them stays its call.
- **Filters** are either facts that cannot be argued with (a task longer than the minutes available, a condition the caller says is not met) or narrowing the agent asks for (one subtree, a minimum priority, a deadline window). Each reports how many tasks it removed. Energy against drain is reported, not filtered.
- **Facts per row:** own and effective priority; "due in N days" with firmness and source; estimate; activation; drain; effective motive; how many tasks it unblocks; days since touched; times deferred; times started; state flags; for recurring tasks, days past wake-up; and a one-line hint from its pattern if it has one.

These facts take over the role of draft 2's `reasons`: the agent can explain any suggestion from them, and you can see what it was working from. If the plain order proves to leave good candidates below the cut, the first fix is a longer shortlist, not a formula.

### 5.11 Filling in fields

A task has about ten descriptive fields: priority, deadline and firmness, estimate, activation, drain, motive, conditions, achievable. You should rarely be asked for any of them.

**Server side:**

- Only the title is required. Every other field may be empty, and an empty field is reported as unknown, never defaulted to a middle value that looks like information.
- `field_sources` records where each value came from: `guess` (the agent's first estimate), `pattern` (copied from similar past work), `evidence` (corrected from completions, deferrals or false starts) or `user` (you said so).
- A `user` value is never overwritten by a guess or by evidence. If evidence disagrees with it, that goes in the review queue as a question for you.

**Agent side, written into the triage skill:**

1. **Guess first.** Fill every field from the title, the parent, matching patterns and the profile. Do not ask before guessing.
2. **Ask only when the answer is both unclear and consequential.** A missing deadline on something that sounds date-bound is worth a question. The drain of "buy milk" is not.
3. **Keep a question budget.** No questions at capture. At most one per task at triage, batched so several tasks are settled in one exchange. Larger questions wait for the weekly review.
4. **Prefer questions that generalise.** "Are phone calls usually this hard to start for you?" settles a whole pattern; the answer is written to the pattern or profile so it is never asked again.
5. **Say what was assumed, briefly,** when it matters: "I've put this down as about an hour and low priority." A correction from you is cheaper than a question to you.

Over time the share of fields filled by `pattern` and `evidence` should rise and the number of questions should fall. Both are countable from `field_sources` and the event log, which gives a direct measure of whether the system is learning you.

## 6. Tools

These are the operations a model can call. They are defined once and offered both by the app and over MCP.

| Tool | Purpose |
|---|---|
| `capture_tasks` | One or more titles, landing in the inbox untriaged. |
| `create_task` | Full creation, including recurrence. |
| `update_task` | Any field, including moving to a new parent; marks triaged. |
| `subdivide_task` | Atomically add children and ordering to a parent. |
| `link_tasks`, `unlink_tasks` | Blocker links, with the cycle check. |
| `start_task` | Logs a start. Feeds the false-start signal. |
| `complete_task` | Sections 5.7 and 5.8. Takes the reflection and pattern. |
| `snooze_task` | Defer with an optional reason; pauses a recurring task. |
| `drop_tasks` | Close without doing, with a reason. Accepts several ids. |
| `reopen_task` | Undo a completion or drop. |
| `merge_tasks` | Fold a duplicate into another task: moves children, links and notes, then closes it. |
| `get_candidates` | The shortlist with facts (section 5.10). Takes minutes available, energy, conditions, and optional narrowing by subtree, priority or deadline window. |
| `get_critical` | High priority or deadline at risk within a horizon, including blocked tasks. |
| `get_task`, `get_tree`, `search_tasks` | Read access. |
| `get_review_queue` | Inbox, pending parent checks, per-task upkeep counts, and pairs of tasks with near-identical titles. |
| `get_recent_activity` | Bounded feed of what happened in a period, plus the want / have-to balance. |
| `find_patterns`, `save_pattern` | Look up digests; edit the `learned` note; merge two patterns. |
| `read_profile`, `write_profile_note` | The charter and general user memory. |
| `get_guide` | Returns a skill's text, for MCP clients that do not surface MCP prompts. The app does not use it: its guides are in the prompt. |

That is about two dozen tools, which is a context cost and a source of wrong picks for a small model. Where two tools prove to be always used together, they should be merged. The cost is highest for an outside assistant that shares its context with other servers, so the merge is reviewed when the MCP adapter is built out in Phase 6.

The three "give me tasks" stories: for the top task or a few options, the agent calls `get_candidates` with current state and picks one or several from the shortlist; everything urgent today is `get_critical(horizon=1 day)`.

## 7. Skills

Each skill is one markdown file. The app puts every skill in its system prompt, so the model always has them. For other assistants the same file is reachable as an MCP prompt, through the `get_guide` tool, and as a `SKILL.md` folder for agent platforms that load skills; whether a given host makes use of any of those is up to the host.

| Skill | What it does |
|---|---|
| Onboarding | First run: explains the model, drafts the charter with you, captures a first batch of tasks, seeds profile notes. |
| Triage | Works through the inbox, filling fields by the guess-first rules in section 5.11 and asking only the few questions that matter. |
| Subdivide | Breaks a task down; decides when to ask the user. |
| What now | Covers the three list stories; asks for current state only when it would change the answer; handles high-activation tasks; when giving several options, spreads them across want and have-to and says which is which. |
| Plan time block | Given a block length, energy and constraints, pulls candidates and returns an ordered plan with buffers, using the balance and the charter to decide how much of the block goes to wants. |
| Complete and reflect | Closes a task, collects the reflection, files the pattern, handles parent and recurrence checks. |
| Morning plan, evening reflection | For unattended runs of the app, started by the system scheduler. |
| Housekeeping | Runs mostly without you, on a small model: re-dates or clears soft deadlines that are long past, merges duplicates, flags untouched tasks, consolidates profile notes. Acts on clear cases, reports what it did, and saves judgement calls for the weekly review. |
| Weekly review | With you: what went stale, what keeps being deferred or false-started, what to drop, reshape or recommit to. |

## 8. User stories

1. I mention something in passing and it is captured in one line without questions.
2. I ask what to do now and get options that fit my time and energy, each with a reason.
3. I ask for the single most important thing and get one answer.
4. I ask what is urgent today and see deadline-driven items even if they are blocked, with what is blocking them.
5. I describe a large, vague effort; we break down only the first layer and leave the rest undefined.
6. I finish a step and am asked how it went; my answer improves later estimates for that kind of work.
7. I say I already did something untracked and it is recorded against the right task.
8. My Friday email appears every Friday whatever I did last week, and a missed one stays visible until I deal with it.
9. My laundry, running and reading come back around loosely, when it makes sense, without me defining rules.
10. I keep failing to start something; the system notices and changes how it offers it.
11. I have a two-hour block and low energy; I get a plan for it.
12. I tell it a suggestion was wrong and it does not make the same mistake next week, in any client.
13. Once a week I am shown what has gone stale and can drop it without guilt.
14. When I get options, I can see which are things I want and which are things I have to do, and over a few weeks the things I want do not quietly disappear.
15. I change one document describing how I want to be treated, and every client's behaviour changes with it.

## 9. Phases

| Phase | Builds | You can now |
|---|---|---|
| 0. Scaffold | Project, schema, migrations, event `record()` helper, test harness, seeded demo database. | Run the test suite. |
| 1. Tasks and graph | CRUD, parents, blockers, cycle check, derived states, candidate pool, FastMCP server with basic tools, `get_guide`. Check Goose's prompt support. | Capture tasks and ask what is available. |
| 2. Prioritisation | Effective values with provenance (priority, deadline, motive), the shortlist with facts and reserved places, `get_critical`, the profile and charter. Triage and What-now skills. | Get ranked, explained suggestions. |
| 2A. The app | The tool catalogue, our own loop with `task-orchard chat`, the MCP adapter rebuilt on the catalogue, and scenarios run against a real model. Added by the 2026-11-15 revision; numbered 2A so the later phases keep their numbers. | Use Task Orchard directly, with no MCP client. |
| 3. Leading edge and completion | `subdivide_task`, parent check, completions, patterns (filing only), `start_task`, snooze, drop, reopen, untracked completion. Subdivide and Reflect skills. | Run a multi-step project end to end. |
| 4. Recurrence | Scheduled mode with `roll_forward`; flexible mode with recurrence checks. | Track fixed obligations, chores and habits. |
| 5. Learning and review | Pattern digests and their use in triage, review queue, recent activity with the balance, `merge_tasks`. Onboarding, Plan-time-block, Housekeeping and Weekly-review skills. Skill scenarios (section 10). Revisit logging of suggestions not chosen. | Plan a time block; see what it has learned; let it tidy itself. |
| 6. The service | One long-running process: FastAPI read routes with program-facing views, MCP over HTTP with a token and a review for outside assistants, the app over HTTP, unattended runs for housekeeping and the morning and evening skills. | Leave it running; have it tidy up and plan the day unasked. |
| 7. Android app | Sketch only: REST for views, a chat channel to the app, reminders and timers. Planned in detail after Phase 6 has been lived with. | — |

Phases 1–3, with 2A, are the prototype. I'd suggest using it daily after Phase 3 before building further, because the agent's ranking and the breakdown skill are the parts most likely to feel wrong in practice.

## 10. Testing

- **Graph invariants** as property tests: no cycle is ever accepted; a candidate is never blocked or snoozed; effective priority never falls below own priority; unlinking restores the previous effective values.
- **Recurrence** as table tests with frozen time: early, late, long absence and the two-instance cap, pause and resume, daylight-saving change, month-end rules; `roll_forward` run twice changes nothing.
- **Shortlist**: pressing tasks always make the cut; a filter hides a task only when the caller applied it, and reports how many; every fact in a row matches the underlying data.
- **Event log**: every mutating service call produces exactly the events the spec lists for it, in the same transaction.
- **Tools** exercised through the catalogue's `call_tool`, including a check on response size. One module proves the MCP adapter exposes the same catalogue.
- **The app's loop** tested with a scripted model: no test in the normal run calls a real one.
- **Skill scenarios** from Phase 2A: a seeded database plus a user message, run through the app against a real model, with the expected tool calls and end state. Moving judgement to the agent moves the risk there too, and this is the only way to tell whether a cheaper model handles a skill well enough or needs a larger one. They replace the manual scripts in Goose that earlier drafts planned.

## 11. Still open

Nothing here blocks Phases 0–3.

1. **What the charter says about balance.** Not a build question, but the reserved places and the balance only pay off once the charter states an intent. Onboarding can leave this blank and the weekly review can raise it once there is a month of data.
2. **Whether activation and drain are the right axes.** Accepted for now. Revisit after a few weeks of use, when the completion reflections show whether the two are actually distinguishable in practice.
3. **Whether `field_sources` earns its keep.** It is the one piece of bookkeeping added for the guess-first approach. If it proves fiddly, the fallback is a single "confirmed by user" flag per task.
4. **Which model runs which skill.** Housekeeping, triage and pattern filing look safe on a small model. Collaborative breakdown of a vague project and time-block planning may not be. The skill scenarios are how this gets answered, and with our own loop the answer can be acted on: the app's model is a setting, and unattended runs get their own. Until Phase 5 completes the scenarios it is a guess.

Deferred by agreement: logging suggestions that were not chosen (Phase 5), tuning when the agent closes parents unasked (later phase), calendar integration, the priority scale.

## 12. Why the app comes first (2026-11-15)

The original plan put the AI in an MCP client and treated the client as a free user interface. Two phases of use showed the cost of that.

**The design describes an assistant, not a tool server.** It has a routine for the start of every conversation, guides to be read before each kind of job, a charter that is meant to govern how the user is treated, a question budget, a model sized to the work, and upkeep that runs with nobody present. An MCP server can ask a host for all of this and enforce none of it. Whether the profile is read, whether a guide is followed, and which model does the reading are the host's decisions. The parts of the product most likely to feel wrong in use, ranking and breakdown, were the parts we could neither control nor test.

**What our own loop settles:**

| Before | With the app |
|---|---|
| The server asks the host to call `read_profile` and read the overview at the start of each conversation. | The profile and every guide are in the system prompt. Nothing is fetched and nothing can be skipped. |
| Guides are offered three ways because hosts differ in what they surface. | One way, always present. |
| "Built for a small model" is a hope about whichever model the host runs. | The model is a setting, and can differ between conversation and unattended upkeep. |
| Housekeeping and the morning and evening skills need an agent platform to run them. | They are runs of the app, started by the system scheduler. |
| Skills are tested by hand, in a host, by reading what it did. | Scenarios run the real prompt and tools against a real model and assert on the calls and the end state. |
| The server cannot see which suggestions the user was shown. | The loop can. |
| The phone app needs "a chat channel to the agent", and there is no agent of ours for it to reach. | The app is that agent. |

**What it costs.** Model calls are now ours to pay for and to get right. A terminal conversation has to be built, and later stored conversations and a chat endpoint. The loop is deliberately small to keep this in check: it holds no task logic and makes no judgement in code.

**Why MCP stays.** It was never the wrong interface, only the wrong centre. It remains the way to reach your tasks from an assistant you are already working in, and to combine them with tools the app does not have, a calendar above all (section 1 keeps calendar integration out of scope on exactly that basis). Behaviour there is best-effort, since an outside assistant may ignore the guides.

**Why this was cheap.** The layering held. `core` imports nothing from the adapter, and the adapter was a few hundred lines of pass-through, so nothing below the tool definitions changes. The posture of section 2 is untouched: the server computes facts and a model judges. What moved is who runs the model.

**The alternative that was considered** was to go the other way and make Task Orchard a more conventional MCP server: no start-of-conversation routine, guidance carried inside tool results, half as many tools, and the server taking over whatever discipline the guides ask for. That is coherent, and it needs no loop or model bill of our own. It was not chosen because it gives up the parts of the original idea that are most its own: an assistant that follows a charter, keeps a question budget, learns the user and checks in. Several of its ideas are still worth having for the MCP adapter, and are listed under Phase 6 in the specification.
