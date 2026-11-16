# Task Orchard — Implementation Plan

**This is the design rationale: why Task Orchard is built the way it is.** The build document is `docs/SPEC.md`, which says what every operation does and is authoritative where the two differ; its section 13 lists the decisions made since.

This began as the full implementation plan, drafted on 2026-11-14 from `INITIAL_PLAN.md`. On 2026-11-15 the app became the primary way in (section 11). The parts the spec now covers in more detail (the data model, the operations, the skills, the phases and the tests) have been removed from here; git history has them. Items marked **(default)** were chosen without discussion and are the cheapest to revisit. Section 10 lists what is still open.

## 1. Decisions

| Topic | Decision |
|---|---|
| Where the AI lives | In a loop above the server: Task Orchard's own (the app), or an outside assistant's. The server itself never calls an LLM. |
| Hosting | One machine, SQLite file. HTTP access is added later without a rewrite. |
| First client | The app: `task-orchard chat`, a terminal conversation with our own prompt, guides and choice of model. (Originally a chat-style MCP host.) |
| MCP and REST | Adapters over the same operations as the app. MCP is for reaching tasks from another assistant; REST is for programs. Both are built out in Phase 6. |
| "Not live yet" | One field, `snooze_until`, replaces both "care about date" and "snooze period". |
| Own vs. effective values | A task stores its own priority and deadline. Effective priority and deadline are derived on every read and never stored, so removing a link reverts them immediately. |
| Recurrence | Exactly one deterministic pattern (a calendar schedule). Every other kind of repetition is flexible and agent-guided (section 5). |
| Missed scheduled instances | They stay open and overdue until completed or explicitly dropped. At most two instances of one scheduled task are live; when a third would spawn, the oldest is closed as `missed`. |
| Server vs. agent | The server owns invariants, arithmetic and retrieval. The agent owns judgement, including ranking (section 2). Routine work is expected to run on a small model such as Haiku. |
| History | Raw completions and events are kept, but the AI reads compact **patterns**, never raw rows (section 6). |
| Flexible recurrence | `gap_days` as a fallback snooze and completing a flexible task directly are both accepted. |
| Firmness | `hard`, `soft`, or no deadline. A dated wish is a soft deadline with low priority. |
| Upkeep | A housekeeping skill tidies overdue soft deadlines, duplicates and untouched tasks. |
| Memory | Lives on the server: patterns for kinds of task, profile notes for the user in general. |
| Task demands | Two axes, `activation` and `drain`: how hard it is to start, and how draining it is to do (section 4). Accepted for now; may be revisited after real use. |
| Filling in fields | Only a title is ever required. The agent guesses the rest, infers from patterns, and asks only where an answer is both unclear and consequential (section 8). |
| Want vs. have-to | A third marker, `motive`, separate from activation. The server stores it and counts the balance of time spent; what to do about the balance is policy, held in the charter (sections 4 and 6). |
| Deadlines | Optional. No deadline is the normal state for wish-level tasks. |
| Parent close | The agent asks, but acts on its own when the answer is obvious. |
| Durations | Self-reported at completion. Starts are logged separately as a resistance signal. |
| Directiveness | The server exposes query primitives; the agent composes them. Time-block planning is a skill. |
| Calendar integration | Out of scope. Later, give an outside assistant a calendar MCP server alongside this one. |
| Priority scale | Four named levels (`low`, `normal`, `high`, `critical`), revisable. |

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
- **No background process.** A `roll_forward(now)` step runs at the start of every request and handles everything time-driven (section 5). Unattended work in Phase 6 is a command the operating system's scheduler runs, not a daemon of ours.
- **Time** is stored in UTC. The user's timezone is config. A date-only deadline means end of that day locally. Schedules are computed in local time so that Friday stays Friday across daylight-saving changes.
- **Stack (default):** Python 3.12+, `uv`, SQLAlchemy 2 with Alembic migrations, SQLite FTS5 for search, pydantic for tool schemas, the `anthropic` SDK for the app's model calls, FastMCP for the MCP adapter, `pytest` with `hypothesis`. FastMCP and the Claude API both move quickly, so check their current docs before using either.

## 4. Task demands and motive

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

- Values are `want`, `should` and `must`. A `should` is self-imposed: nobody else is waiting on it, but you would hold yourself to it (exercise, flossing, keeping up with other teams' docs). A work project you happen to enjoy is still a `must`; the enjoyment goes in its pattern note.
- The agent sets it at triage and you can correct it. Unlike activation and drain there is no evidence loop.
- The server's part is facts only: the effective motive on every row, reserved shortlist places so wants are not cut (section 7), and the balance of time spent (section 6). How much to protect wants, and when, is policy in the charter.

## 5. Recurrence

The mechanics are in the spec's section 7. The reasoning behind them:

- **Two modes, and only one of them is a rule.** A calendar schedule ("every Friday", "the 1st of each month") is the one kind of repetition the server can run by itself, from the clock. Everything else ("laundry about every five days", "run twice a week", "read other teams' docs when there's time") is a judgement about when to come back, so the rule is free text the agent reads.
- **Scheduled tasks keep lockstep with the calendar.** Spawning depends only on the clock, never on when the last instance was finished. Completing Friday's instance on Wednesday, or on Saturday, changes nothing about when next Friday's appears.
- **Missed instances stay, up to a cap of two live.** An instance not done by its slot remains open and overdue next to the newer one, because a missed obligation should stay visible until it is dealt with. The cap stops a long absence from burying the user: after three weeks away there are two instances, and the history shows the rest as missed. Dropping one by hand needs a reason and is logged as a cancellation, which is distinct from `missed`.
- **A pause is not a run of misses.** Snoozing a scheduled task skips the paused slots and logs one paused span. **(default)**
- **`gap_days` is a fallback, not a schedule.** It exists so that a chore just finished does not reappear at once when the agent sets nothing. The agent can leave the default, choose another return time, or create a concrete instance with its own deadline ("laundry, ideally by Thursday").
- **A flexible task can be offered and completed as itself** ("go for a run?"). Creating an instance first is optional ceremony, useful when the occurrence needs its own deadline or breakdown.
- **Quotas such as "twice a week" are not a third rule type.** The agent handles them from the cadence text and recent history.

## 6. History and memory

Keeping every completion is about storage; what the model reads is a separate question. The design separates the two.

**Stored underneath (never read raw by the model):**

- `completions`: one row per finished task.
- `events`: one row per change. The service layer has a single `record()` helper, called inside the same transaction as every mutation, so the log cannot drift from the data.

**What the model reads:**

- **Patterns** are the merged archive the original idea asked for. A pattern is a named kind of work: "performance reviews", "exercise", "phone calls to book things". At completion, the agent files the task under an existing pattern or creates one. A pattern digest is fixed-size:
  - computed on read from the rows underneath: times done, dropped, deferred, false starts; typical actual time against estimate; typical felt activation and drain; last done;
  - a short `learned` note the agent maintains ("draining; goes better in the morning; takes about twice the estimate").
- **The charter** is one pinned profile note: your own statement of what Task Orchard is for and how it should treat you. It is the equivalent of a `SOUL.md`, kept on the server so every client reads the same one. This is where policy lives, such as how much of a week should go to wants or whether a want may be offered as a reward after a must. Onboarding drafts it with you, and it can start nearly empty and grow.
- **Profile notes** hold what is true of you in general, including corrections to suggestions ("no chores before noon", "low energy after 3pm"). They are capped in total size; housekeeping consolidates them.
- **Recent activity** is a bounded feed from the event log, for evening reflection and weekly review. It includes the **balance**: time spent over the last 7 and 30 days split by want, should and must, from the durations reported at completion.

**How it is used:**

| Moment | Memory consulted |
|---|---|
| Session start | The charter and profile notes. The app puts them in its prompt; an MCP host is told by the server's instructions to read them. |
| Presenting options or planning a block | The balance, read against the charter. |
| Triage of a new task | Matching patterns seed the estimate, activation and drain. So "do a performance review" starts out marked draining because the last ones were. |
| Winnowing candidates | Profile notes; pattern digests of the shortlisted tasks. |
| You correct a suggestion | The agent writes a profile note or updates the pattern's `learned` note. |
| Review queue | Event-derived counts per task: times deferred, starts without a finish, days untouched, days past a soft deadline. The server reports the numbers; the agent decides what is worth raising. |
| An agent makes a wrong change | The event log shows what changed, and `reopen_task` or `update_task` undoes it. |

**Why not an existing MCP memory server.** Memory stays in Task Orchard because patterns are joined to task rows, which an external store cannot do; there is one file to back up; and every client behaves the same. Running a general-purpose memory server next to it remains possible.

## 7. Shortlisting

An early draft had a weighted scoring formula here. That was a judgement heuristic in server code, so it was replaced by a shortlist: the server's job is to cut the pool to about 20 rows and attach the facts, and the agent ranks.

- **Order** is deliberately plain and has no tuned weights: pressing hard deadlines first, then anything due soon, then the rest; within each, by effective priority and then deadline. It only decides what makes the cut.
- **Reserved places.** Wants are typically low priority with no deadline, so the plain order alone would never show them. A few places in every shortlist are held for the top `want` candidates. This guarantees the agent sees them; whether to offer them stays its call.
- **Filters** are either facts that cannot be argued with (a task longer than the minutes available, a condition the caller says is not met) or narrowing the agent asks for (one subtree, a minimum priority, a deadline window). Each reports how many tasks it removed. Energy against drain is reported, not filtered.
- **Facts per row** let the agent explain any suggestion, and let you see what it was working from: priority, deadline with its source, estimate, activation, drain, motive, what the task unblocks, how long since it was touched, how often it was deferred or started, and a hint from its pattern.

If the plain order proves to leave good candidates below the cut, the first fix is a longer shortlist, not a formula.

## 8. Filling in fields

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

## 9. User stories

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

## 10. Still open

Nothing here blocks Phases 0–3.

1. **What the charter says about balance.** Not a build question, but the reserved places and the balance only pay off once the charter states an intent. Onboarding can leave this blank and the weekly review can raise it once there is a month of data.
2. **Whether activation and drain are the right axes.** Accepted for now. Revisit after a few weeks of use, when the completion reflections show whether the two are actually distinguishable in practice.
3. **Whether `field_sources` earns its keep.** It is the one piece of bookkeeping added for the guess-first approach. If it proves fiddly, the fallback is a single "confirmed by user" flag per task.
4. **Which model runs which skill.** Housekeeping, triage and pattern filing look safe on a small model. Collaborative breakdown of a vague project and time-block planning may not be. Moving judgement to the agent moves the risk there too, and the skill scenarios are the only way to tell whether a cheaper model handles a skill well enough. With our own loop the answer can be acted on: the app has a basic and a strategic model setting, both Haiku by default, and either can be raised without code changes. Until Phase 5 completes the scenarios it is a guess.
5. **How many tools.** About two dozen tools is a context cost and a source of wrong picks for a small model. Where two prove to be always used together, they should be merged. The cost is highest for an outside assistant that shares its context with other servers, so the merge is reviewed when the MCP adapter is built out in Phase 6.

Deferred by agreement: logging suggestions that were not chosen (Phase 5), tuning when the agent closes parents unasked (later phase), calendar integration, the priority scale.

**Use it before building further.** Phases 1–3, with 2A, are the prototype. Use it daily after Phase 3 before going on, because the agent's ranking and the breakdown skill are the parts most likely to feel wrong in practice.

**After the spec.** Phase 7, an Android app, is a sketch only: REST for views, a chat channel to the app, reminders and timers. It is planned in detail after Phase 6 has been lived with.

## 11. Why the app comes first (2026-11-15)

The original plan put the AI in an MCP host and treated the host as a free user interface. Two phases of use showed the cost of that.

**The design describes an assistant, not a tool server.** It has a routine for the start of every conversation, guides to be read before each kind of job, a charter that is meant to govern how the user is treated, a question budget, a model sized to the work, and upkeep that runs with nobody present. An MCP server can ask a host for all of this and enforce none of it. Whether the profile is read, whether a guide is followed, and which model does the reading are the host's decisions. The parts of the product most likely to feel wrong in use, ranking and breakdown, were the parts we could neither control nor test.

**What our own loop settles:**

| Before | With the app |
|---|---|
| The server asks the host to call `read_profile` and read the overview at the start of each conversation. | The profile and every guide are in the system prompt. Nothing is fetched and nothing can be skipped. |
| Guides are offered three ways because hosts differ in what they surface. | One way, always present. |
| "Built for a small model" is a hope about whichever model the host runs. | Two model settings, basic and strategic, both Haiku by default. The user picks the tier, and either can be pointed at a larger model. |
| Housekeeping and the morning and evening skills need an agent platform to run them. | They are runs of the app, started by the system scheduler. |
| Skills are tested by hand, in a host, by reading what it did. | Scenarios run the real prompt and tools against a real model and assert on the calls and the end state. |
| The server cannot see which suggestions the user was shown. | The loop can. |
| The phone app needs "a chat channel to the agent", and there is no agent of ours for it to reach. | The app is that agent. |

**What it costs.** Model calls are now ours to pay for and to get right. A terminal conversation has to be built, and later stored conversations and a chat endpoint. The loop is deliberately small to keep this in check: it holds no task logic and makes no judgement in code.

**Why MCP stays.** It was never the wrong interface, only the wrong centre. It remains the way to reach your tasks from an assistant you are already working in, and to combine them with tools the app does not have, a calendar above all (section 1 keeps calendar integration out of scope on exactly that basis). Behaviour there is best-effort, since an outside assistant may ignore the guides.

**Why this was cheap.** The layering held. `core` imports nothing from the adapter, and the adapter was a few hundred lines of pass-through, so nothing below the tool definitions changes. The posture of section 2 is untouched: the server computes facts and a model judges. What moved is who runs the model.

**The alternative that was considered** was to go the other way and make Task Orchard a more conventional MCP server: no start-of-conversation routine, guidance carried inside tool results, half as many tools, and the server taking over whatever discipline the guides ask for. That is coherent, and it needs no loop or model bill of our own. It was not chosen because it gives up the parts of the original idea that are most its own: an assistant that follows a charter, keeps a question budget, learns the user and checks in. Several of its ideas are still worth having for the MCP adapter, and are listed under Phase 6 in the specification.
