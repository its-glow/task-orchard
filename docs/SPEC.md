# Task Orchard — Feature Specification

This is the build document. It says exactly what each part does, so that implementation needs no further design decisions. `IMPLEMENTATION_PLAN.md` explains why the design is what it is; read it once for context. Where the two differ, this document wins (section 13 lists the refinements).

**Status.** Phases 0 (scaffold and schema), 1 (tasks and graph), 2 (prioritisation), 2A (the app), 3 (leading edge and completion), 4 (recurrence) and 5 (learning and review) are done and tested; the eleven scenarios pass on Claude Haiku 5.5, and the strategic ones also on Claude Sonnet 5.5. Phase 6 is specified here and not yet built.

**How it is used.** Task Orchard has its own assistant, called the app: a loop that talks to a model and calls the operations of section 8 directly (section 10.2). The app is the primary way in, and behaviour is designed and tested against it. Other assistants reach the same operations over MCP, and programs over REST; both are adapters built out in Phase 6 (section 12).

## Contents

1. Ground rules
2. Glossary
3. Time
4. Wire conventions
5. Derived state
6. History and memory
7. Recurrence
8. Operations
9. Skills
10. Tools, the app and the MCP adapter
11. Phases and acceptance
12. Phase 6: the service
13. Refinements over the plan
14. Future work

---

## 1. Ground rules

1. **The server computes facts; the agent makes judgements. This is a default, not a prohibition.** The server (`core` and the database) owns invariants, arithmetic, the clock and retrieval, and never calls an LLM. Model calls happen only in the app (section 10.2), which sits above `core` exactly as an outside assistant does. Retrieval already involves simple heuristics: choosing which 20 of 200 tasks the model sees is one (section 5.7). A server-side heuristic is acceptable when all of these hold:
   - it decides what to **show**, not what to do;
   - it can be stated in a sentence;
   - it is visible: the response says what was left out and why;
   - the caller can adjust or switch it off with a parameter.

   What stays out of the server is opaque scoring: weighted formulas whose result the agent can neither explain nor override. When unsure, start with the agent deciding, and move logic into the server once real use shows the agent is being handed too much or is deciding badly.
2. **Built for a small model.** Assume the caller is Haiku-class: by default the app runs on Claude Haiku 5.5 throughout (section 10.2). Facts arrive ready to use. Tools validate everything and explain failures with a hint. A multi-step change is one atomic call. The strategic tier may be pointed at a larger model, but the tools and guides must not depend on one: the scenarios of section 10.2 run on the defaults to check this.
3. **Derived, not stored.** Nothing in section 5 or 6.1 is ever written to a column.
4. **Context is a budget.** Every response is bounded (limits are stated per tool). Lists return compact rows. History reaches the model only as aggregates.
5. **Every mutation is logged** in the same transaction as the change (section 6.4).
6. **Layering.** `core/` imports nothing from `tools`, `agent/`, `mcp/` or `api/`. A tool or route calls one `Orchard` method and returns its dict. All logic is in `core/`. `tools` (the catalogue, section 10.1) imports only `core`. `agent/`, `mcp/` and `api/` are peers above it and import nothing from each other.
7. **The app comes first.** Behaviour is designed for, and tested through, Task Orchard's own assistant, where the prompt, the guides and the model are under our control. An outside assistant connected over MCP gets the same tools and the same facts, but may not read the guides or the charter; it is supported on a best-effort basis.

## 2. Glossary

| Term | Meaning |
|---|---|
| Task | A row in `tasks`. May have children. A **plain** task is one that is not recurring. |
| Recurring task | A task with `recur_mode` set, to `scheduled` or `flexible`. A container for its occurrences: never completed, only dropped (retired) or snoozed (paused), and has no `achievable` value. There is no separate type field: being recurring is having a mode. |
| Instance | A task whose parent is a recurring task. One occurrence of it. Otherwise an ordinary new task: it starts with a title and nothing copied, and its fields are filled in the way any new task's are. |
| Slot | One date and time produced by a scheduled task's rule. A scheduled instance stands for exactly one slot (`occurrence_at`). |
| Open | `status = open`. Closed means `done`, `dropped` or `missed`. |
| Leaf | An open task that is not recurring and has no open children. Every use of "leaf" in this document means exactly this. |
| Own value | What is stored on the task itself. |
| Effective value | What the task has once inheritance through parents and blockers is applied (section 5.4). |
| Dormant | Hidden by a snooze on the task or on any ancestor. |
| Blocked | Waiting on an open blocker of the task or of any ancestor. |
| Candidate | A task that could be worked on now (section 5.3). |
| Inbox | Tasks with `triaged = false`: captures, and scheduled instances the clock has spawned (section 7.1). |
| Pattern | A named kind of work, with statistics and a short learned note. |
| Charter | The profile note with topic `charter`: the user's statement of what Task Orchard is for. |
| Row | The compact JSON form of a task used in lists (section 4.3). |
| Detail | The full JSON form of one task (section 4.4). |
| The server | `core` and the database: everything that stores tasks and computes facts. It never calls a model. |
| Tool | One operation of section 8 as a model sees it: a name, a description and a parameter schema. Defined once, in the catalogue (section 10.1). |
| The app | Task Orchard's own assistant: the loop in `agent/` that sends the conversation to a model and runs the tools it asks for (section 10.2). `task-orchard chat` is its terminal form. |
| The agent | Whatever model-driven loop is calling the tools and making the judgements: the app, or an outside assistant connected over MCP. Where this document says "the agent decides", it means either. |
| Session | One conversation with the app, from its start to `/new` or exit. |
| Guide | A skill's markdown file (section 9), as the agent reads it. |

## 3. Time

### 3.1 Storage and the clock

- Every instant is stored in UTC and handled as an aware `datetime`. The one exception is `Task.recur_dtstart` (section 7.1).
- `tx.now` is the only source of the current time. Nothing calls `datetime.now()`.
- The user's timezone is `tx.tz`. All parsing and formatting below use it.
- **Where the timezone comes from**, first match wins: `TASK_ORCHARD_TZ` in the environment; the zone stored in the database (from Phase 5, set by `set_timezone` once onboarding has confirmed it with the user); `TZ` or the system zone; UTC. Storing it means two clients started with different environments still agree on what "today" is.

### 3.2 Parsing input: `parse_when(text, tz, now, day_means) -> datetime`

Every tool parameter that takes a point in time accepts exactly these forms. Matching ignores case and surrounding whitespace.

| Form | Example | Meaning |
|---|---|---|
| ISO date | `2026-11-20` | That day. |
| ISO date and time | `2026-11-20T17:00`, `2026-11-20 17:00` | That local wall-clock time. |
| ISO with offset | `2026-11-20T17:00Z`, `...+01:00` | That exact instant. |
| `today`, `tomorrow` | | That day. |
| Weekday name | `friday`, `fri` | The next such day strictly after today. |
| Offset from now | `+3d`, `+2w`, `+90m`, `+4h` | Days and weeks give a day; minutes and hours give an exact instant. |

- Any day form may be followed by a time: `friday 17:00`, `tomorrow 09:30`, `+3d 12:00`.
- **What a day with no time means depends on what the parameter is for.** `parse_when` takes this as `day_means`:

  | Parameter | A bare day means |
  |---|---|
  | `deadline` | The end of that day: 23:59. |
  | `snooze_until`, `until` | The start of that day: 00:00. Something snoozed "until Friday" is back on Friday morning. |
  | `completed_at` | 12:00 that day, or `now` if that would be in the future. |
- A local time that does not exist (spring forward) moves forward to the next valid minute. An ambiguous one (fall back) takes the first occurrence.
- Anything else raises `invalid` with the hint: `Use a date like 2026-11-20, a date and time like 2026-11-20T17:00, a weekday name, "tomorrow", or an offset like +3d.`

No other natural language is parsed. This grammar exists so the model never has to work out a calendar date from a weekday.

### 3.3 Formatting output

A **stamp** is how every instant is returned:

```json
{"at": "2026-11-20T17:00", "day": "Fri", "rel": "in 2 days"}
```

- `at` is local wall-clock time to the minute, with no offset. It is valid input to `parse_when`.
- `day` is the three-letter English weekday of `at`.
- `rel` comes from `relative(instant, now)`:

| Distance | Future | Past |
|---|---|---|
| under 1 minute | `now` | `now` |
| under 60 minutes | `in N min` | `N min ago` |
| under 48 hours | `in N hours` (`in 1 hour`) | `N hours ago` |
| otherwise | `in N days` | `N days ago` |

N is rounded down. For a deadline that has passed, `rel` is `overdue by N days` (or hours, or min) instead of `N days ago`.

### 3.4 Calendar days

"Today" runs from local midnight to local midnight. "Within N days" in this document means within N × 24 hours of `now` unless it says "calendar days". Every "within" includes its end: a task due in exactly 48 hours is within two days. That holds for `due_within_days`, `horizon_days`, `DUE_SOON_HOURS` and the at-risk window alike.

**A snooze counted in days ends at the start of a day.** When a snooze is set by a number of days (`snooze_task(days=)`, or a flexible task's `gap_days`), it ends at local midnight at the start of the day that falls that many × 24 hours after the starting instant. A run finished on Monday evening with a gap of 2 is back on Wednesday morning, not Wednesday evening.

Day counts are whole numbers. A fraction would make this rule misbehave: half a day from a morning ends at the start of the same day, which is a wake, not a snooze.

## 4. Wire conventions

### 4.1 General

- Every tool returns one JSON object. Every object includes `now`: a stamp without `rel`. (`get_guide` is the one exception.)
- **Enumerated values are exchanged by name**: `"high"`, `"must"`, `"soft"`. Numbers are never used for scales. Input names are matched leniently: case is ignored, surrounding whitespace is trimmed, and spaces and hyphens count as underscores, so `"Must"` and `" high "` are accepted. An unknown name raises `invalid` and lists the valid names.
- **Empty keys are omitted.** A key whose value would be null, `[]`, `{}`, `false` or `0` is left out, unless this document says "always present".
- Titles in rows are truncated to 80 characters with a trailing `…`. Titles inside nested references (`via`, `blocked_by`, `priority_via`, `path`, `parent`) are truncated to 40. Both limits include the `…`.
- Parameter names that take an id end in `_id` (`task_id`, `parent_id`, `pattern_id`), and the same names are used in `clear`.
- Ids are integers. A parameter that takes several ids is a JSON array.

### 4.2 Errors

The service raises `OrchardError(code, message, hint, details)`. The catalogue's `call_tool` (section 10.1) turns it into this JSON, which the app hands to the model as a failed tool result and the MCP adapter returns as a tool error:

```json
{"error": "cycle", "message": "That would create a loop ...", "hint": "Remove one of ...", "details": {"path": [...]}}
```

Codes are fixed in `core/enums.py::ErrorCode`. Every error has a `hint` that tells the model what to do next. A failed call changes nothing.

`core` converts every failure it can foresee into an `OrchardError`: enum parse failures become `invalid`, and database integrity errors become the matching code. Anything else reaching `guarded` is a bug.

| Code | Raised when |
|---|---|
| `not_found` | An id does not exist. |
| `invalid` | An argument is malformed, out of range, or combined illegally. |
| `cycle` | A link, move or subdivision would make tasks wait on each other. `details.path` is the loop. |
| `not_open` | The operation needs an open task and this one is closed. Hint names `reopen_task`. |
| `already_open` | `reopen_task` on an open task. |
| `has_open_children` | `complete_task` on a task with open children. `details.children` lists them. |
| `parent_closed` | Reopening or creating under a closed parent. |
| `instance_exists` | A flexible recurring task already has an open instance. |
| `instance_cap` | Reopening a scheduled instance would exceed two live instances. |
| `unsupported` | A combination this version deliberately does not handle. The hint gives the alternative. |
| `profile_full` | A profile write would exceed the size limits. |

### 4.3 The task row

Used in every list. Built by `views.task_row`. Keys appear in this order; all are subject to the omit-when-empty rule.

| Key | Content |
|---|---|
| `id`, `title` | Always present. |
| `status` | Omitted when `open`. |
| `instance_of` | Id of the recurring parent, for instances. |
| `path` | Titles of the nearest three ancestors, from the highest of them down to the parent, each truncated to 40 characters. When there are more than three, the first entry is `…`, so the list has four entries. Omitted for roots. |
| `priority` | Effective priority. Omitted when neither the task nor anything it inherits from sets one (section 5.4). |
| `priority_via` | `{id, title, how}` when the effective priority is not the task's own. `how` is `"parent"` or `"blocks"`. |
| `priority_own` | The task's own priority, only when it differs from the effective one. |
| `due` | The earliest effective deadline: a stamp plus `firmness`, plus `via` (as above) when inherited. |
| `hard_due` | Same shape. Present only when `due` is soft and a later hard deadline also applies. |
| `estimate_minutes` | |
| `activation`, `drain` | |
| `motive` | Effective motive. |
| `achievable` | `"no"` or `"unknown"`. Omitted when `yes`, and always omitted for recurring tasks, which have no achievable value (section 7.3). |
| `state` | Array of flags that apply: `inbox`, `dormant`, `blocked`, `has_open_children`, `overdue`, `at_risk`. |
| `snoozed_until` | Stamp, when dormant by the task's own snooze. |
| `blocked_by` | Up to 3 open effective blockers as `{id, title}`, plus `blocked_by_more: n` if there are others. |
| `unblocks` | Count of open tasks this task explicitly blocks. |
| `conditions` | At most 5 tags of at most 30 characters each (section 8.2). |
| `touched` | `rel` of `last_touched` (section 5.6), when more than 1 day ago. Omitted for recurring tasks. |
| `deferred` | Times deferred (section 5.6). |
| `starts` | Times started (section 5.6). |
| `recurring` | For recurring tasks: `{mode, cadence, gap_days, last_done, done_count, past_wake_days, next_slot}` as applicable (section 7). |
| `pattern` | `{id, name, hint, ratio}`. `name` is cut to 40 characters like a nested title. `hint` is the pattern's `learned` note cut to 120 characters. `ratio` is the estimate ratio (section 6.1) when it rests on at least 3 completions. |

List tools may add keys to a row; each tool says which.

### 4.4 The task detail

Returned by `get_task` and by every write that changes one task. Built by `views.task_detail`. It is the row, plus:

| Key | Content |
|---|---|
| `description` | |
| `notes` | Cut to 2,000 characters; `notes_truncated: true` if cut. |
| `fields` | The task's **own** values and where each came from: `{"priority": {"value": "low", "source": "user"}, ...}` for every tracked field that is set. |
| `parent` | `{id, title}`. |
| `children` | Open children as `{id, title, state}`, at most 20, plus `children_open` and `children_closed` counts. |
| `blocks` | Tasks this one explicitly blocks: `{id, title, status}`. |
| `blocked_by` | In the detail, all effective open blockers, each with `via` when inherited from an ancestor. |
| `completion` | For done tasks: `{completed (stamp), actual_minutes, felt_activation, felt_drain, reflection}`. |
| `recent_events` | The last 10 events on the task: `{type, when (rel), note}`. `note` is a one-line summary built from the payload (for example `until 2026-11-20, "not today"`). |
| `created` | Stamp. |
| `closed` | Stamp, for closed tasks. |
| `pattern` | In the detail, the full digest (section 6.1). |

### 4.5 Field sources

The tracked fields are those in `enums.TRACKED_FIELDS`: `priority`, `deadline` (which covers firmness), `estimate_minutes`, `activation`, `drain`, `motive`, `conditions`, `achievable`.

- `create_task`, `update_task` and `subdivide_task` take an optional `sources` object: `{"deadline": "user", "estimate_minutes": "pattern"}`. A tracked field that is written without an entry is recorded as `guess`.
- An entry for a field the call does not write is ignored, and so is an entry naming a real field that carries no source (`enums.UNTRACKED_FIELDS`: `recurrence`, `title`, `notes` and the like). Any other name is `invalid`, because it is almost always a typo that would otherwise record a user value as a guess. `firmness` is accepted as another name for `deadline`.
- Writing a field with the same value but a new source (the user confirming a guess) is a change, and is logged.
- **Protection.** If a field's current source is `user` and the incoming source for it is not `user`, that field is left unchanged. The rest of the call still applies. The response lists each such field under `skipped`: `{field, kept, proposed, reason: "set by the user"}`, where a deadline's `kept` and `proposed` are `{at, firmness}`. If the incoming source was `pattern` or `evidence`, a `conflict` event is also written (section 6.4).
- **Clearing is protected the same way.** A field named in `clear` counts as written with the source given for it in `sources`, `guess` if none. So clearing a `user` field needs `sources: {field: "user"}`; otherwise it is skipped and reported with `proposed: null`.
- **A skip is impossible to miss.** Whenever `skipped` is present, the response also has a top-level `hint`: `Some fields were not changed because the user set them. If the user asked for this change, repeat the call with sources={"<field>": "user"}.` A small model relaying the user's own words would otherwise report a change that did not happen.
- A successful clear removes the field's source entry.
- An untracked field (title, notes, snooze, pattern, parent) has no source and is never protected.
- An unset field is unknown. It is never defaulted to a middle value, and it is omitted from rows.

## 5. Derived state

Everything here is computed per request from one in-memory snapshot of the database: all open tasks, their ancestors, and all blocker links. Implement in `core/graph.py` and `core/derive.py` as pure functions over that snapshot.

### 5.1 The must-finish-first graph

Nodes are tasks. A directed edge `A → B` means A must be finished before B can be. There are three sources of edges:

1. **Child before parent:** `child → parent` for every task with a parent.
2. **Blocker before blocked:** `blocker → blocked` for every row in `task_blocks`.
3. **Inherited blocking:** `blocker → D` for every descendant D of `blocked`.

The set of tasks that X **effectively blocks** is: the tasks X explicitly blocks, plus all their descendants.

### 5.2 Cycle checks

The graph over **all tasks, whatever their status**, must be acyclic at all times. Including closed tasks means reopening can never create a cycle.

Any operation that adds an edge (linking, moving to a new parent, creating with `blocked_by` or `blocks`, subdividing with ordering, merging) is checked like this:

1. Build the full edge set of 5.1 with the proposed change applied.
2. Search for a cycle.
3. If one exists, raise `cycle`. `details.path` is the loop as a list of `{id, title, status, because}`, starting with the edge being added so the change reads first, where `because` is `"child of"`, `"blocks"` or `"blocks an ancestor of"` and describes the edge to the next entry. `status` is included because the loop may run through closed tasks the model has not seen in any list. The hint names `unlink_tasks`, which works on closed tasks too (section 8.5).

Building the whole graph per check is intended. It is obviously correct and the data is small.

Cases that must be rejected (each needs a test):

- A blocks B, B blocks A.
- A blocks its own ancestor's blocker chain: X blocks P, A is a child of P, A blocks X. (Explicit links alone show no loop; the inherited edge `X → A` closes it.)
- A task blocking its own ancestor, or its own descendant.
- Moving a task under its own descendant.
- Moving T under P when a blocker of P (or of P's ancestors) is itself waiting on T or on one of T's descendants.

### 5.3 Predicates

For a task T at time `now`:

| Predicate | Definition |
|---|---|
| `open` | `status = open`. |
| `dormant` | T or any ancestor has `snooze_until > now`. |
| `blocked` | T or any ancestor has an explicit blocker that is open. A dormant blocker still blocks. |
| `has_open_children` | At least one child is open. |
| `is_instance` | T's parent is a recurring task. |
| `candidate` | `open` and `triaged` and not `dormant` and not `blocked` and not `has_open_children`, and T is either a plain task or a flexible recurring task. |

Consequences worth stating:

- A scheduled recurring task is never a candidate. Its instances are.
- A flexible recurring task is a candidate exactly when it is awake and has no open instance.
- An inbox task is never a candidate.
- A candidate may have `achievable = no` or `unknown`. That is the leading edge: the row says so and the agent breaks it down.

### 5.4 Effective values

All three are computed by memoised recursion along the edges of 5.1, which is safe because the graph is acyclic. Only open tasks take part; a closed task contributes nothing, and has nothing to inherit: its own row shows its own priority, deadline and motive.

**Effective priority** of T is the highest of:

- T's own priority, if set;
- the effective priority of T's parent;
- the effective priority of every open task T effectively blocks.

If none of these exists, the task has no effective priority: the key is omitted from rows, and wherever tasks are ordered or filtered by priority it counts as `normal`. The source is reported as `via`: the parent, or the blocked task that supplied the value (the explicitly blocked one, even when the value came up through its descendant). Ties prefer own, then parent, then blocked tasks in id order.

**Effective deadline** is computed twice, once over all deadlines and once over hard deadlines only, giving `due` and `hard_due`. Each is the earliest of:

- T's own deadline (for the hard pass, only if its firmness is hard);
- the effective deadline of T's parent;
- for every open task B that T effectively blocks: B's effective deadline minus `remaining_minutes(B)`.

Firmness and source travel with the winning value; `via` and ties work exactly as for priority. `hard_due` is reported only when it is later than `due`.

**Effective motive** is T's own motive, or else that of the nearest ancestor that has one. It does not travel through blocker links.

Required property (test it): removing a link or moving a task changes effective values on the next read with no other action.

### 5.5 Remaining work, overdue and at risk

- `remaining_minutes(T)`: if T has open children, the sum of `remaining_minutes` over them; otherwise T's `estimate_minutes`, or 0 if unset.
- A deadline is **overdue** when it is in the past.
- A deadline is **at risk** when it is not overdue and falls within the larger of 24 hours and `3 × W` from now. W is the work that has to fit before that deadline: `remaining_minutes` of the **highest ancestor of T, or T itself, that has the same effective deadline** in that pass. That owner is found by walking up from T for as long as the parent's effective deadline in the same pass is the same instant.
  - When T inherits a project's deadline unchanged, W is the whole project's remaining work. A project due in three days with thirty hours of open work makes every one of its leaves at risk now, not on the last day.
  - When T's deadline is earlier than its parent's (its own, or pulled earlier through a blocker link), W is T's own remaining minutes.

Both tests are applied to `due` for the `overdue` and `at_risk` flags in rows and for the critical list. They are applied to the hard-pass deadline of 5.4 (whether or not it is reported as `hard_due`) for the `pressing` group of the shortlist.

The factor of 3 stands for the fact that only part of any stretch of wall-clock time is workable. It and the other two constants below live in `core/shortlist.py` and nowhere else:

```
AT_RISK_MIN_HOURS = 24
AT_RISK_WORK_FACTOR = 3
DUE_SOON_HOURS = 72
```

### 5.6 Per-task counts

Computed from `events`:

| Fact | Definition |
|---|---|
| `deferred` | Number of `snoozed` events on the task. |
| `starts` | Number of `started` events on the task. |
| `last_touched` | The latest event on the task that shows the user engaging with it: any event whose type is in `enums.TOUCH_EVENTS` (`created`, `started`, `snoozed`, `woken`, `subdivided`, `reopened`, `completed`), or an `updated` event in which at least one change was written with source `user`. If none, `created_at`. |

"Touched" exists to measure staleness for the review queue and the weekly review. Edits the agent makes on its own (guesses at triage, pattern fill-ins, housekeeping clearing a stale deadline) and everything the clock does are deliberately not touches. Otherwise tidying would make a task the user has ignored for months look fresh, hiding exactly what the weekly review is meant to surface. Being offered in a shortlist and passed over is not a touch either; whether to record it as its own signal is an open question (section 14).

`update_task` setting `snooze_until` writes `updated`, not `snoozed`, and so does not count as a deferral. This is deliberate: `snooze_task` means "the user is putting this off"; `update_task` is how the agent schedules when something should come back (section 7.3).

### 5.7 The shortlist (`get_candidates`)

Implement in `core/shortlist.py`. There are no weights.

1. **Pool:** all candidates.
2. **Filters**, applied only when the caller supplies the parameter, in this order:
   - `under_id`: keep only that task and its descendants.
   - `min_priority`: drop tasks whose effective priority is lower.
   - `due_within_days`: keep only tasks whose `due` is overdue or within that many days. Tasks with no deadline are dropped.
   - `minutes_available`: drop tasks whose `estimate_minutes` is set and larger. Tasks with no estimate stay.
   - `exclude_conditions`: drop tasks having any of the listed condition tags.

   A task removed by more than one filter is counted under the first that removed it. Everything after this step, including the reserve for wants, works on what the filters left.
3. **Group** each remaining task:
   - `pressing`: its hard-pass deadline is overdue or at risk (section 5.5);
   - `due_soon`: overdue on any deadline, or `due` within `DUE_SOON_HOURS`;
   - `other`: everything else.
4. **Order** by: group (pressing, due_soon, other); effective priority, highest first; `due`, earliest first, none last; `created_at`, oldest first; id.
5. **Cut** to `limit`.
6. **Reserve wants.** The reserve is `min(reserve_wants, limit // 3)`, so a short list stays mostly the top of the order: `limit=1` or `2` reserves nothing, and `limit=5` reserves one place. If fewer rows than the reserve have effective motive `want`, bring in the highest-ordered `want` candidates that were cut, each replacing the lowest-ordered row that is in group `other` and is not a `want`. Stop when the reserve is met, when no cut `want` remains, or when no replaceable row remains. Re-sort by the order of step 4. Rows brought in this way carry `reserved: true`.

Each row carries, besides section 4.3: `group`, and `over_energy: true` when the caller gave `energy` and the task's `drain` is higher. Energy never filters.

The response also reports what was left out, so the agent knows the shortlist is partial (section 8.13).

### 5.8 The critical list (`get_critical`)

All **leaves** (section 2), whether or not they are blocked, dormant or in the inbox, for which any of these holds:

- overdue;
- at risk;
- `due` within `horizon_days`;
- effective priority is `critical`.

Inbox tasks are included because spawned scheduled instances wait in the inbox until triaged (section 7.1), and one with a hard deadline must not go unseen. Captures rarely have a deadline or priority, so this adds little else. Such rows carry the `inbox` state flag.

Ordered by `due` (none last), then effective priority, highest first, then id. At most 30 rows, with `omitted` if more. Rows include up to 5 open effective blockers as `blocked_by`, each with `via` when inherited, plus `blocked_by_more: n` if there are others. Explaining what stands in the way is the point of this tool, so this list is longer than a normal row's.

## 6. History and memory

Implement in `core/history.py`. Raw completions and events never leave this module; only the aggregates below do.

### 6.1 Pattern digest

A pattern's members are the tasks with that `pattern_id`, **excluding recurring tasks themselves** (they are containers; their instances are members).

| Key | Definition |
|---|---|
| `id`, `name`, `description`, `learned` | Stored. |
| `done`, `dropped`, `missed`, `open` | Member counts by status. |
| `deferred` | `snoozed` events across members. |
| `false_starts` | For done members, `max(0, starts − 1)`; for dropped members, `starts`; summed. |
| `actual_minutes` | Median `actual_minutes` over the 20 most recent completions that have one. |
| `ratio` | Median of `actual_minutes / estimate_at_completion` over the 20 most recent completions that have both and whose `estimate_source_at_completion` is `guess` or `user`, to one decimal place. `ratio_samples` gives the count. Estimates sourced from `pattern` or `evidence` are excluded because they were derived from past completions, so including them would feed the ratio back into itself. |
| `felt_activation`, `felt_drain` | Median over the 20 most recent completions that have the value, rounded to the nearest level (ties round up), as a name. |
| `last_done` | Stamp of the most recent completion. |
| `gaps_days` | Days between consecutive completions, most recent 5 gaps, one decimal place. Only when the pattern has at least 2 completions. |

Medians and `gaps_days` are given to one decimal place, and a whole number is written without one (`45`, not `45.0`). `description` is cut to 600 characters, the limit `save_pattern` enforces (section 8.17).

A digest is at most this fixed set of keys. It is the only form in which history reaches the model.

### 6.2 Profile and charter

- One note per topic; topics compare without regard to case. A topic is at most 100 characters, with spacing collapsed. A replaced note takes the topic's new spelling.
- Topic `charter` is the charter. Limit 4,000 characters.
- Every other note: limit 1,000 characters each, 8,000 in total.
- A write that would exceed a limit raises `profile_full`, with the current sizes in `details` and the hint to consolidate existing notes first.
- Writing the same text again, or deleting a topic that does not exist, changes nothing and writes no event.

### 6.3 The balance

For each of the windows "last 7 days" and "last 30 days" (by `completed_at`), under the keys `last_7_days` and `last_30_days`:

```json
{"minutes": {"want": 240, "should": 90, "must": 1260, "unset": 45},
 "completions": {"want": 2, "should": 3, "must": 19, "unset": 1},
 "estimated": 4, "unknown_duration": 2}
```

- Grouping is by `motive_at_completion`; null goes to `unset`.
- A completion's minutes are its `actual_minutes`; if that is unset, its `estimate_at_completion`, and it is counted in `estimated`; if both are unset it contributes 0 minutes and is counted in `unknown_duration`.
- Here, and only here, zero values are **not** omitted.

### 6.4 Events

Written only through `core.events.record()`, with `at = tx.now`. The table below is exhaustive: an operation writes exactly these events, and a call that changes nothing writes none.

| Operation | Events (task the event is on) and payload |
|---|---|
| `capture_tasks` | `created` (each new task) `{via: "capture"}` |
| `create_task` | `created` (new task) `{via: "create", parent_id}`; `linked` per link; `updated` (parent) when its `achievable` was flipped (section 8.2); `updated` (recurring parent) `{changes: {snooze_until}}` when creating an instance under a sleeping flexible task woke it (section 7.2) |
| `update_task` | `updated` (task) `{changes: {field: {from, to, source?}}}`, where `source` is present for tracked fields and is the source the value was written with, and a deadline's value is `{at, firmness}`; `description` and `notes` are logged by length instead, as `{from_chars, to_chars}`, so long text is not copied into the log; plus `paused` or `resumed` (task) when a scheduled task's own snooze is set or cleared; plus `conflict` (task) `{field, kept, proposed, source}` per protected field skipped with source `pattern` or `evidence`; plus `updated` (new parent) when a move flipped its `achievable` |
| `subdivide_task` | `created` (each child) `{via: "subdivide", parent_id}`; `linked` per ordering link; `subdivided` (parent) `{children: [ids]}`; `updated` (parent) when its `achievable` was flipped (section 8.2) |
| `link_tasks` | `linked` (blocked task) `{blocker_id, blocked_id}` |
| `unlink_tasks` | `unlinked` (blocked task) `{blocker_id, blocked_id}` |
| `start_task` | `started` (task) `{}` |
| `complete_task` | `completed` (the task that became done) `{actual_minutes?, completed_at, pattern_id?, recurring_id?, sleeping_until?}`, where `pattern_id` is present when the call filed the task under a pattern; preceded by `created` `{via: "complete", parent_id}` when the task was created by this call, or by `spawned` when a scheduled task's next instance was created by this call; preceded by `dropped` `{reason: "parent completed", cascade_from}` for each open child dropped; plus `updated` (parent) when a task created by this call under `parent_id` flipped the parent's `achievable` (section 8.2); plus `pattern_saved` when `new_pattern` created one |
| `snooze_task` | `snoozed` (task) `{until, reason?, previous?}`; or `woken` (task) `{previous}`, and nothing when waking a task that has no snooze; for a scheduled recurring task `paused` `{until, reason}` or `resumed` `{skipped_until}` instead |
| `drop_tasks` | `dropped` (each task, including descendants) `{reason, cascade_from?}` |
| `reopen_task` | `reopened` (task) `{was, completion?}` where `completion` holds the deleted completion row's fields |
| `merge_tasks` | `merged` (source) `{into, moved_children, moved_links, filled}`; `updated` (target) `{changes}` if any of its fields changed |
| `save_pattern` | `patterns_merged` (no task) `{from: {id, name}, into, tasks_moved}` when merging; then `pattern_saved` (no task) `{pattern_id, created?, changes}` when the pattern was created or a field changed. `changes` holds `name` and `learned` as `{from, to}` and `description` by length as `{from_chars, to_chars}` |
| `write_profile_note` | `profile_saved` (no task) `{topic, deleted?}` (the text is not copied into the log) |
| `roll_forward` | `spawned` (instance) `{recurring_id, occurrence_at}`; `missed` (instance) `{recurring_id, replaced_by}`, preceded by `dropped` `{reason: "instance missed", cascade_from}` for each of its open descendants; `resumed` (recurring) `{skipped_until}`; `dropped` (recurring) `{reason: "schedule ended"}` when its rule has no slots left (section 7.1) |
| `set_timezone` | `settings_saved` (no task) `{timezone: {from, to}}` |

`{from, to}` values use the wire form (names, stamps' `at` strings). A deadline's value is `{at, firmness}` wherever a payload carries one (`updated`, `conflict`).

### 6.5 The review queue (`get_review_queue`)

Each section is `{items, total}`. `items` holds at most 10 entries, except `inbox`, which holds up to 30 so triage can work in batches. The server applies no thresholds beyond "greater than zero"; it sorts and cuts, and the agent decides what is worth raising.

Items are **slim rows**: `{id, title, path?}` plus the keys the section names, not the full row of section 4.3. The agent calls `get_task` for anything it decides to act on. This keeps the queue inside the response budget.

| Section | Contents, in order |
|---|---|
| `inbox` | Untriaged open tasks, oldest first. Each adds `instance_of` for spawned instances. |
| `parent_checks` | Open plain tasks that have at least one child and no open children. |
| `overdue` | Leaves whose own deadline has passed, longest overdue first. Each adds `overdue_days` and `firmness`. |
| `most_deferred` | Open tasks with `deferred > 0`, highest first. Each adds `deferred`. |
| `repeat_starts` | Open tasks with `starts > 1`, highest first. Each adds `starts`. |
| `untouched` | Triaged leaves, by `last_touched`, oldest first. Each adds `untouched_days`. Dormant tasks and recurring tasks (containers) are excluded. |
| `duplicates` | Pairs of open tasks whose normalised titles are equal: `{a: {id, title}, b: {id, title}}`, with `a` the lower id. Three tasks with one title make three pairs. Ordered by `a`, then `b`. A pair within one recurring family (a recurring task and its instance, or two instances of the same task) is not a duplicate: instances share their recurring task's title by design. |
| `conflicts` | `conflict` events from the last 30 days on tasks that are still open: `{task: {id, title}, field, kept, proposed, source, when}`. |
| `missed` | Per scheduled recurring task with instances missed in the last 30 days: `{recurring: {id, title}, missed: n, done: n}`, both counted over the same 30 days, most missed first. |

A **normalised title** is case-folded, with runs of whitespace collapsed and punctuation removed from either end. `Call Mum!` and `call  mum` are equal; `call-mum` is a different title.

### 6.6 Recent activity (`get_recent_activity`)

For the last `days` (1 to 31):

- `completed`: up to 30 `{id, title, actual_minutes, motive, when}`, newest first, plus `completed_count`.
- `dropped`, `missed`: up to 10 each as `{id, title, when, reason?}`, newest first, plus counts. A task counts when its status is still `dropped` or `missed` and its closing event (`dropped`, `merged` or `missed`) falls in the window, so a task dropped and then reopened is not listed. A merged task's `reason` is `merged into #<id>`.
- `created_count`, `deferred_count`, `started_count`.
- `balance`: section 6.3, always both windows whatever `days` is.

## 7. Recurrence

Implement in `core/recurrence.py`. Only this module and `complete_task` create instances.

### 7.1 Scheduled tasks

Stored fields: `rrule`, `recur_dtstart`, `lead_minutes`, `recur_firmness`, `last_spawned_slot`, and optionally `cadence` as a human description.

**The rule.**

- `rrule` is an RFC 5545 RRULE value such as `FREQ=WEEKLY;BYDAY=FR`. A leading `RRULE:` is stripped. Text containing `DTSTART` or a newline is rejected.
- `FREQ` must be `DAILY`, `WEEKLY`, `MONTHLY` or `YEARLY`. Anything more frequent is rejected with `invalid`.
- `BYHOUR`, `BYMINUTE` and `BYSECOND` are rejected with `invalid`: the time of day comes from `due_time` alone, and these parts would let a daily rule produce several slots a day (`FREQ=DAILY;BYHOUR=9,17`). The hint says to use `due_time`, or two recurring tasks.
- `UNTIL` is accepted as a date (`20261231`) or a date and time with or without `Z`. A date alone means the end of that day, so "until 31 December" includes a slot on the 31st at any due time. A `Z` value is converted to local wall-clock time before parsing, because the rule is evaluated on naive local time and dateutil rejects a UTC `UNTIL` there.
- The rule must parse with `dateutil.rrule.rrulestr` and must produce at least one slot after `now`.
- `BYMONTHDAY=31` skips months without a 31st. The hint, and the triage skill, steer to `BYMONTHDAY=-1` for "the last day of the month". The `next_slots` in the create response let the agent see what a rule really does.
- **A rule that runs out** (through `COUNT` or `UNTIL`) retires its task: when `roll_forward` finds no next slot for an open scheduled task, and the task has no open instances, it drops the task with reason `schedule ended`. While instances remain open, it waits.
- It is evaluated on **naive local wall-clock time**, starting from `recur_dtstart`. Each slot is then localised to the user's timezone (section 3.2 rules for missing and ambiguous times) and converted to UTC. This keeps "Friday 17:00" at 17:00 local across daylight-saving changes.
- `recur_dtstart` is set when the rule is created or changed: today's local date at the rule's due time. The due time comes from the `due_time` parameter (`HH:MM`, default `23:59`).
- A rule with no `BYDAY`, `BYMONTHDAY` or similar therefore repeats from the day it was created. The create response shows the next three slots so the agent can confirm the rule means what the user meant.

`next_slot(R, after)` is the first slot strictly later than `after`. `after` is converted to local wall-clock time before the rule is consulted.

**Creation.** `last_spawned_slot = now`, so the first instance is for the first slot after creation. `create_task` does not spawn it: `roll_forward` has already run for that request, so the first instance appears on the next request whose `now` is inside the lead window.

**`roll_forward(session, now, tz)`.** Runs at the start of every request. For each open scheduled task R, in id order:

```
if R.snooze_until is set:
    if R.snooze_until > now: skip R                      # paused
    R.last_spawned_slot = max(R.last_spawned_slot, R.snooze_until)
    R.snooze_until = None
    record RESUMED on R {skipped_until}                  # the pause has ended

spawned = 0
slot = next_slot(R, after=R.last_spawned_slot)
while slot exists and slot − lead <= now and spawned < 60:
    live = R's open instances, oldest occurrence first
    if len(live) >= 2:
        drop every open descendant of live[0]            # reason "instance missed"
        close live[0]: status = missed, closed_at = now
        record MISSED on it {recurring_id, replaced_by: <new instance id>}
    create the instance for `slot`
    record SPAWNED on it {recurring_id, occurrence_at}
    R.last_spawned_slot = slot
    slot = next_slot(R, after=slot)
    spawned += 1
```

- An **instance** is created with: R's `title`; `parent_id = R.id`; `occurrence_at = slot`; `deadline = slot`; `deadline_firmness = R.recur_firmness`; `triaged = false`. The deadline is recorded with source `user`, since it comes from the rule the user set, so a later guess cannot move it. Nothing else is copied. Priority and motive reach it from R by ordinary inheritance (section 5.4), as for any child. Every other field starts unset, exactly as for a captured task.
- **Spawned instances wait in the inbox.** Nobody is in the conversation when the clock creates an instance, so it is in the same position as a capture, and the triage skill fills it in the same way (from its parent, its previous occurrences, matching patterns and the profile). While untriaged it is not a candidate, but `get_critical` includes it (section 5.8), so a deadline cannot go unseen.
- An instance that was broken down and is then closed as `missed` takes its open descendants with it: they are dropped with reason `instance missed`, so no open task is left under a closed parent.
- **Idempotent:** calling it twice with the same `now` changes nothing the second time. The unique index on `(parent_id, occurrence_at)` backs this up.
- **Lockstep:** spawning depends only on the clock and `last_spawned_slot`, never on when an instance was completed.
- **Cap:** at most two open instances per scheduled task. After a long absence the loop walks every due slot, so the skipped ones exist in history as `missed`.
- The limit of 60 per request only bounds one request; the next request continues.
- Only R's **own** snooze pauses it. A snooze on an ancestor hides R's instances but does not stop them spawning.

**Pausing.** Setting R's own `snooze_until` (through `snooze_task` or `update_task`) is a pause: `paused` is logged, open instances become dormant, and nothing spawns. When the pause ends, by the clock or by waking, `last_spawned_slot` jumps forward to the moment the pause ended (never backward), so the paused slots are skipped, and `resumed` is logged.

**Completing R directly.** `complete_task` on a scheduled recurring task means "this occurrence is done":

- if R has open instances, the oldest is completed;
- otherwise the next slot's instance is created now, as in the spawn rule but triaged, since it is completed at once (`spawned` is logged); `last_spawned_slot` moves to that slot, and that instance is completed. This is the "did Friday's email on Tuesday" case.

Completing a scheduled task, directly or through one of its instances, returns `next_slot`: the stamp of the first slot after `last_spawned_slot`, which is the next occurrence still to be spawned. The `recurring` block of a scheduled task's row has `mode`, `cadence`, `last_done`, `done_count` and `next_slot`; while R is paused, `next_slot` is the first slot after the pause ends.

`start_task` on a scheduled task is `invalid`, with a hint to start its open instance: the occurrences are what get worked on. A flexible task can be started as itself.

**Changing the rule** (`update_task` with `recurrence`): `recur_dtstart` is recomputed from today, `last_spawned_slot` becomes `max(last_spawned_slot, now)`, and existing instances are left alone.

Manual children cannot be added to a scheduled task (`unsupported`).

### 7.2 Flexible tasks

Stored fields: `cadence` (free text for the agent) and optionally `gap_days`.

**Completion.** When an instance of flexible R is completed, or R is completed directly:

1. If R was completed directly and has an open instance, that instance is the one completed. If it has none, a done instance is created under it to hold the completion: R's title and nothing else copied, triaged, no `occurrence_at`, no deadline, filed under the call's `pattern_id` or `new_pattern` if given. R itself stays open.
2. If `gap_days` is set, `R.snooze_until` is set from `completed_at` and `gap_days` by the rule in section 3.4. If not, `snooze_until` is left as it is.
3. The response carries a **recurrence check**:

```json
{"recurring": {<row of R>},
 "cadence": "twice a week, never two days in a row",
 "gap_days": 2,
 "sleeping_until": {<stamp>},
 "recent": [{"when": "1 day ago"}, {"when": "5 days ago"}, ...],
 "gaps_days": [4.0, 4.1, 3.0],
 "note": "Adjust when this comes back with update_task(snooze_until=...), or create an instance under it with its own deadline."}
```

`recent` lists up to 5 completions, newest first.

**What the agent can then do** (all through ordinary tools):

- nothing: the fallback snooze stands;
- `update_task(R, snooze_until=...)`: choose a different return time. This is scheduling, not a deferral, and is not counted as one;
- `create_task(parent_id=R, ...)`: spawn a concrete instance. It is an ordinary new task: the title defaults to R's, and nothing else is copied; the agent passes whatever fields it wants, as for any create. At most one open instance may exist (`instance_exists`). If R is sleeping (its `snooze_until` is in the future), creating the instance clears R's snooze, since the agent has now chosen the next occurrence explicitly. Otherwise the new instance would be hidden by its parent's snooze until R woke, even when its own deadline came first. This is scheduling, not a deferral, and is logged as `updated` on R.

**In lists.** The `recurring` block of a flexible task's row has `mode`, `cadence`, `gap_days`, `last_done` (rel), `done_count`, and `past_wake_days`: whole days since `snooze_until`, when `gap_days` is set and the task is awake.

`snooze_task` on a flexible task, or on its instance, **is** a deferral and is counted.

**Offered as itself.** A flexible task with no open instance is a candidate in its own right ("go for a run?"). Its own `estimate_minutes`, `activation`, `drain` and `conditions` describe one occurrence, and are what its row shows. They are not copied to instances.

### 7.3 Shared rules

- A recurring task cannot be a blocker (`unsupported`: block with its instance instead). It can be blocked; its instances inherit that.
- A recurring task's parent cannot be recurring. An instance cannot be recurring.
- A recurring task may have an ordinary parent. While it is open it counts as an open child, so that parent is not a leaf.
- Dropping a recurring task retires it and drops its open instances.
- **A recurring task has no `achievable` value.** It is a container for occurrences, not a step. Rows omit the key, `create_task` and `update_task` reject `achievable` for it (`invalid`), and the rule of section 8.2 that adding a child flips the parent to `no` does not apply to it. Each instance has its own `achievable`, like any task.
- **Patterns are references, never automatic.** A recurring task and its instances are filed under a pattern only when the agent chooses one, as for any other task (at triage, or at completion through `complete-and-reflect`). The recurrence check, `last_done`, `done_count` and the gaps come from R's instances directly and need no pattern.
- A plain task can be converted to recurring through `update_task(recurrence=...)` if it is open, has no children, is not an instance, blocks no open task (a recurring task cannot be a blocker), and has no deadline (or the same call clears it; a deadline set by the user needs `sources` `user` on that call, as for any protected field). Its `achievable` value is removed. The reverse is `unsupported`.
- A change to the recurrence keys is logged in the `updated` event as `changes.recurrence: {from, to}`, each side the task's recurrence keys in wire form (`null` on conversion).

## 8. Operations

Each operation is one `Orchard` method and one tool of the same name in the catalogue (section 10.1), which makes it available to the app and to the MCP adapter alike. "Phase" says when it is built. Parameters marked `?` are optional. Unless stated, a task id that does not exist raises `not_found`.

Every write returns the affected task as a detail (section 4.4) under `task`, unless stated otherwise.

### 8.1 `capture_tasks` — Phase 1

Put things in the inbox with no questions asked.

- `items`: 1 to 20 of `{title, notes?}`.
- Creates each as an open, untriaged, root task.
- Returns `{captured: [{id, title}], possible_duplicates: [{new_id, existing: {id, title}}]}`. A possible duplicate is an existing open task with the same normalised title (section 6.5). Items earlier in the same call count as existing tasks, so a brain-dump that repeats itself is flagged. At most three matches are reported per new item.

### 8.2 `create_task` — Phase 1 (recurrence in Phase 4)

Create a triaged task with whatever is known.

| Parameter | Notes |
|---|---|
| `title` | Required, non-blank, at most 500 characters. Optional when the parent is a flexible recurring task, in which case it is copied from the parent. |
| `description?`, `notes?` | `notes` is at most 20,000 characters, however it is written (here, `append_note`, or a merge). A write that would exceed it is `invalid`, with the hint to rewrite the notes shorter with `update_task(notes=...)`. |
| `parent_id?` | Must be open (`parent_closed`). |
| `priority?` | `low`, `normal`, `high`, `critical`. |
| `deadline?`, `firmness?` | `deadline` per section 3.2. `firmness` defaults to `soft`. In `create_task`, `firmness` without `deadline` is `invalid`. (For `update_task`, see section 8.3.) |
| `snooze_until?` | Section 3.2. |
| `achievable?` | `yes`, `no`, `unknown` (default). |
| `estimate_minutes?` | Integer above 0. |
| `activation?`, `drain?` | `low`, `medium`, `high`. |
| `motive?` | `want` (the user wants to), `should` (self-imposed; nobody else is waiting), `must` (someone else requires it). |
| `conditions?` | Array of tags. Each is lowercased and trimmed, with spaces turned into hyphens. At most 5 tags, each at most 30 characters; more is `invalid`, with the hint that a condition is a short tag. |
| `pattern_id?` | |
| `blocked_by?`, `blocks?` | Arrays of ids; each must be open. Cycle-checked together with the parent. A recurring task may not be among `blocked_by` (section 7.3). |
| `recurrence?` | Section 8.2.1. |
| `sources?` | Section 4.5. |

Rules:

- **A task with children is never achievable.** Achievable is a property of one step, and a task with children is a container. So:
  - Adding a child to a task by any route (here, through `subdivide_task`, by a move, by `merge_tasks`, or by `complete_task(title=, parent_id=)` logging done work under it) sets that parent's `achievable` to `no` if it was not already. This is structural, so it applies even to a `user`-sourced value; the source becomes `evidence`.
  - `update_task(achievable="yes")` on a task that has open or done children is `invalid`, with the hint: `A task with children is not one step. Add a child for the remaining work, or complete this task.` Dropped and missed children do not count, so a breakdown that was tried and abandoned does not lock the task.
  - When all children are done and one step remains ("now submit it"), that step becomes another child.
  - Recurring tasks are outside this rule because they have no `achievable` at all (section 7.3).
- If the parent is a flexible recurring task, this spawns an instance (section 7.2): an ordinary new task whose title defaults to the parent's. If it is scheduled, `unsupported`.
- `recurrence` together with `deadline`, or with a recurring parent, is `invalid`.
- Returns `{task}`; for a scheduled task also `next_slots`: the next three slots as stamps.

#### 8.2.1 The `recurrence` object

```json
{"mode": "scheduled", "rrule": "FREQ=WEEKLY;BYDAY=FR", "due_time": "17:00",
 "lead_days": 2, "firmness": "hard", "cadence": "every Friday by 5pm"}

{"mode": "flexible", "cadence": "about every five days", "gap_days": 5}
```

| Key | Mode | Default |
|---|---|---|
| `mode` | both | Required. |
| `cadence` | both | None. |
| `rrule` | scheduled | Required. |
| `due_time` | scheduled | `23:59`. |
| `lead_days` | scheduled | `1`. May be fractional; stored as `lead_minutes`. |
| `firmness` | scheduled | `hard`. |
| `gap_days` | flexible | None. A whole number, 1 or more (section 3.4). |

A key given for the wrong mode is `invalid`.

### 8.3 `update_task` — Phase 1 (recurrence in Phase 4)

Change any stored field of a task.

- `task_id`, then any of the `create_task` field parameters except `blocked_by` and `blocks`.
- `append_note?`: text added to the end of `notes` on a new line. Together with `notes`, or with `clear: ["notes"]`, it is `invalid`.
- `clear?`: array of field names to unset. Allowed: `description`, `notes`, `priority`, `deadline`, `snooze_until`, `estimate_minutes`, `activation`, `drain`, `motive`, `conditions`, `pattern_id`, `parent_id`, `cadence`, `gap_days`. These are the same names as the parameters that set them. A name that is also being set in the same call is `invalid`, and so is clearing `cadence` or `gap_days` on a task that is not recurring. Clearing a `user`-sourced field is protected (section 4.5).
- **Deadline and firmness.** Passing `firmness` alone changes the firmness of the existing deadline; with no deadline set it is `invalid`. Passing `deadline` alone keeps the existing firmness, or `soft` if there was no deadline. Clearing `deadline` clears its firmness.
- `triaged?`: default `true`. So any update takes a task out of the inbox unless `triaged: false` is passed.
- `recurrence?`: on a plain task, converts it (section 7.3). On a recurring task, a partial object changes only the keys given; `mode` cannot change.

Rules:

- Omitted means unchanged. Unsetting is done only through `clear`.
- Setting `parent_id` moves the task. The new parent must be open; the move is cycle-checked. Instances cannot be moved (`unsupported`), and nothing can be moved under a recurring task this way.
- On a **closed** task only `description`, `notes`, `append_note` and `pattern_id` may change, and `triaged` is left as it is. Anything else is `not_open`.
- Protected fields are skipped and reported (section 4.5).
- Returns `{task, skipped?, hint?}`. A call that changes nothing returns the task and writes no event.

### 8.4 `subdivide_task` — Phase 3

Break a task into children in one atomic call.

- `parent_id`: an open plain task. A recurring task is `unsupported` (subdivide its instance).
- `children`: 1 to 12 of `{title, description?, estimate_minutes?, achievable?, priority?, activation?, drain?, conditions?, after?}`.
- `after?`: positions (counting from 1) of other children in this same call that must be finished first.
- `sequential?`: default `false`. When `true`, each child is blocked by the one before it, and `after` must not be used.
- `sources?`: applies to every child.

Effects: children are created triaged; the parent's `achievable` becomes `no`; ordering links are created and cycle-checked. A closed parent is `parent_closed`. A parent that already has children may be subdivided again; the new children are added alongside. A child with a key not listed above is `invalid`, and every error about a child names its position (`Child 2: ...`).

Returns `{parent: row, children: [rows]}`.

### 8.5 `link_tasks`, `unlink_tasks` — Phase 1

- `blocker_id`, `blocked_id`.
- `link_tasks`: both must be open; they must differ; the blocker must not be recurring; cycle-checked. Linking an existing link is not an error: the response adds `already_linked: true` to the two rows.
- `unlink_tasks`: works whatever the status of either task, because links to closed tasks still count in the cycle check (section 5.2) and must be removable. Removing a link that does not exist returns `{was_linked: false}`.
- Both return `{blocker: row, blocked: row}`, so the caller sees the new effective values.

### 8.6 `start_task` — Phase 3

Record that the user is starting work.

- `task_id`: an open task, or an open flexible recurring task. A scheduled recurring task is `invalid` with the hint to start its instance.
- Writes `started`. A blocked or dormant task may be started; the user's actions are facts.
- Returns `{task, starts}`.

### 8.7 `complete_task` — Phase 3 (recurrence in Phase 4)

Record that something was done.

| Parameter | Notes |
|---|---|
| `task_id?` | The task that was done. |
| `title?` | Instead of `task_id`: create a new task with this title and complete it (untracked work). Exactly one of the two. |
| `parent_id?`, `motive?`, `description?` | Only with `title`. A recurring `parent_id` is `invalid`: pass the recurring task as `task_id` instead. The parent must be open; the new done child flips its `achievable` to `no` (section 8.2). |
| `completed_at?` | Section 3.2; default now. Must not be in the future. |
| `actual_minutes?` | Integer, 0 or more. |
| `felt_activation?`, `felt_drain?` | `low`, `medium`, `high`. |
| `reflection?` | The user's words on how it went. |
| `pattern_id?` | File the task under this pattern. |
| `new_pattern?` | Or under a new pattern with this name. If a pattern with that name exists (ignoring case) it is used. Not together with `pattern_id`. |
| `drop_open_children?` | Default `false`. |

Rules:

- The task must be open (`not_open`).
- With open children: `has_open_children`, unless `drop_open_children` is `true`, in which case every open descendant is dropped first with reason `parent completed`.
- A blocked or dormant task can be completed.
- A recurring target follows section 7.1 or 7.2.
- Writes the `completions` row with `estimate_at_completion`, `estimate_source_at_completion` (the estimate's entry in `field_sources`, `guess` if it has none, null if there is no estimate) and `motive_at_completion` (the **effective** motive at this moment).
- Sets `status = done` and `closed_at = now`. When the work was actually done is `completed_at` on the completion row, which may be earlier.

Returns `{completed: row}` plus, when they apply:

- `parent_check`: when the parent is a plain task, is open and now has no open children: `{parent: row, children: [{title, status}]}` (up to 10, most recently closed first).
- `recurrence_check`: section 7.2.
- `next_slot`: a stamp, for a scheduled instance.
- `unblocked`: rows of tasks for which this was the last open effective blocker: every open task this call's closed tasks (the target and any children it dropped) effectively blocked, that was blocked before the call and is not after it. Descendants freed through a blocked parent are included. At most 10 rows, in id order, plus `unblocked_more: n`.

### 8.8 `snooze_task` — Phase 3

The user is putting something off, or a recurring task is being paused.

- `task_id`: must be open.
- `until?` (section 3.2) or `days?` (a whole number, 0 or more, ending at the start of a day as in section 3.4). Exactly one.
- `reason?`.
- If the resulting time is not after `now` (including `days: 0`), the snooze is cleared instead: this is how to wake a task. Waking a task that has no snooze changes nothing and writes no event.
- `triaged` is left as it is, here and in `start_task`.
- Events per section 6.4. Returns `{task}`.

### 8.9 `drop_tasks` — Phase 3

Close tasks that will not be done.

- `task_ids`: 1 to 50. `reason`: required, non-blank.
- Each open task is set to `dropped` with `closed_at = now`, and so is every open descendant.
- An id that is already closed is not an error; it is listed in `skipped`.
- An id listed twice counts once. A listed task that is also a descendant of another listed task is dropped in its own right and appears in `dropped`, not `also_dropped`.
- Returns `{dropped: [{id, title}], also_dropped: [{id, title}], skipped?: [{id, title, status}], parent_checks?, unblocked?, unblocked_more?}`. `dropped` is always present. `parent_checks` is a list of the `parent_check` objects of `complete_task`, one per open plain parent left with no open children, leaving out parents dropped in the same call. `unblocked` is as in `complete_task`: rows of tasks for which a dropped task was the last open effective blocker.

### 8.10 `reopen_task` — Phase 3

Undo a completion, drop or miss.

- The task must be closed (`already_open`). Its parent must be open (`parent_closed`, with the hint to reopen the parent first).
- Sets `status = open`, clears `closed_at`, deletes the completion row (its fields are kept in the event payload).
- A scheduled instance cannot be reopened if its recurring task already has two open instances (`instance_cap`).
- Reopening a flexible instance while its recurring task has another open instance is `instance_exists`.
- Reopening a scheduled recurring task sets `last_spawned_slot` to the later of its current value and `now`, so slots that passed while it was retired are skipped. Its instances stay as they were.
- Does not undo a flexible task's gap snooze; the response says so in `note` when that applies.
- Returns `{task, note?}`.

### 8.11 `merge_tasks` — Phase 5

Fold a duplicate into the task that should survive.

- `source_id`, `into_id`: both open, different, neither recurring nor an instance (`unsupported`). Neither may be an ancestor of the other.
- The source's children move under the target. The source's blocker links are re-pointed at the target; links that would duplicate an existing one or point at the target itself are discarded. The result is cycle-checked.
- For each tracked field the target lacks and the source has, the value and its source entry are copied. So is `pattern_id`, if the target has none.
- The source's title, description and notes are appended to the target's notes under a line `Merged from #<id>: <title>`. If that would pass the 20,000-character limit on notes, the appended part is cut to fit and ends with `… (cut to fit; see #<id>)`; the source stays readable.
- If open children moved, the target becomes `achievable: no`, as with any other way of adding a child (section 8.2).
- The source becomes `dropped`. It is logged as `merged`, not `dropped` (section 6.4).
- Returns `{task}` (the target).

### 8.12 `get_task`, `get_tree`, `search_tasks` — Phase 1

**`get_task(task_id)`** returns `{task}` as a detail.

**`get_tree(root_id?, depth?, include_closed?)`**

- `root_id` omitted: the forest of root tasks. `depth` 1 to 5, default 2. `include_closed` default `false`.
- Returns `{tree: [nodes], truncated?}` of nested nodes `{id, title, status?, recurring?, state?, children?, more?}`, where `recurring` is the mode of a recurring task and `more` counts descendants not shown, whether below the depth limit or cut by the node limit. Roots are level 1.
- With `root_id`, `tree` holds that one node. A closed `root_id` is shown even when `include_closed` is false.
- At most 100 nodes; if cut, `truncated: true`. The cut is breadth-first, so upper levels stay whole. Order within a level: open before closed, then id.

**`search_tasks(query?, status?, inbox_only?, limit?)`**

- `status`: `open` (default), `closed` or `any`. `limit` 1 to 30, default 20.
- `query` is matched with full-text search over title, description and notes. Split it into words, quote each one, and require all of them; if that finds nothing, retry requiring any of them. Never pass raw user text to `MATCH`. The index uses the `porter` tokenizer, so "spec" also finds "specs".
- A query with no words left once punctuation and FTS syntax are removed (`"*"`, `"--"`, `"_"`) is `invalid`, with the hint to search for words from the title, or to omit `query` to list recent tasks. A blank query (empty or only whitespace) counts as omitted.
- With a `query`, results are ordered by relevance (`bm25`), then `updated_at`, newest first. With no `query`, lists by `updated_at`, newest first.
- Returns `{results: [rows], total}`; both keys are always present. Rows always include `status`.

### 8.13 `get_candidates` — Phase 2

The shortlist of section 5.7.

| Parameter | Notes |
|---|---|
| `limit?` | 1 to 30, default 20. |
| `under_id?` | Narrow to one subtree: "what's next on the release?" The id must exist (`not_found`). |
| `min_priority?` | `low`, `normal`, `high`, `critical`. Narrow to effective priority at or above this. |
| `due_within_days?` | Number from 0 to 365, fractions allowed. Narrow to what is overdue or due in that window. |
| `minutes_available?` | Integer above 0. |
| `exclude_conditions?` | Array of tags. |
| `energy?` | `low`, `medium`, `high`. Marks rows; never filters. |
| `reserve_wants?` | 0 to 10, default 3. The places actually reserved are `min(reserve_wants, limit // 3)` (section 5.7). |

The three narrowing filters exist so the agent can ask a sharper question when the default cut would be the wrong 20. For "is anything urgent", `get_critical` is the better tool, because it also shows urgent work that is blocked.

```json
{"now": {...},
 "candidates": [ <rows with group, reserved?, over_energy?> ],
 "pool": {"total": 34, "shown": 20, "cut": {"pressing": 0, "due_soon": 0, "other": 14}},
 "filtered": {"too_long": 3, "conditions": 1},
 "conditions_in_pool": ["at-home", "business-hours", "good-weather"],
 "inbox": 3}
```

`pool`, `inbox` and `conditions_in_pool` are always present, and the counts inside `pool` and `filtered` keep their zeros. `pool.total` counts candidates after filtering. `pool.cut` always has all three groups; a row displaced by a reserved want counts as cut in its group, so `total = shown + cut`. `filtered` has one key per filter the caller applied (`outside`, `below_priority`, `not_due`, `too_long`, `conditions`) and none for the others. `conditions_in_pool` lists every tag carried by a candidate before filtering, so the agent knows which conditions are worth asking the user about.

### 8.14 `get_critical` — Phase 2

Section 5.8. `horizon_days?`: 0 to 30, default 1. Returns `{critical: [rows], omitted?, inbox}`.

### 8.15 `get_review_queue` — Phase 5

Section 6.5. No parameters. Returns one key per section, each `{items: [...], total}`.

### 8.16 `get_recent_activity` — Phase 5

Section 6.6. `days?`: 1 to 31, default 1 (the last 24 hours). Outside that range is `invalid`.

### 8.17 `find_patterns`, `save_pattern` — Phase 3 (digest statistics in Phase 5)

**`find_patterns(query?, limit?)`**: full-text search over name, description and learned, with the same query handling as `search_tasks`. With no query: most recently used first (by latest member completion), then patterns with no completed member, newest first. `limit` 1 to 10, default 5. Returns `{patterns: [digests]}`.

**`save_pattern(pattern_id?, name?, description?, learned?, merge_from_id?)`**

- Without `pattern_id`: creates a pattern; `name` is required. If the name exists (ignoring case), `invalid` with the existing id in `details`.
- With `pattern_id`: changes the fields given.
- `learned` is at most 600 characters (`invalid` if longer, with the hint to rewrite it shorter, not to append). So is `description`.
- `merge_from_id`: moves that pattern's tasks to `pattern_id` and deletes it. The agent supplies the combined `learned` in the same call. The merge happens before any rename, so the surviving pattern may take the merged one's name. Merging a pattern into itself, or `merge_from_id` without `pattern_id`, is `invalid`.
- Blank `description` or `learned` clears it. Names are trimmed with spacing collapsed, and are at most 200 characters.
- Returns `{pattern: digest}`.

In Phase 3 a digest contained the stored fields and the status counts only; Phase 5 added the remaining statistics.

### 8.18 `read_profile`, `write_profile_note` — Phase 2

**`read_profile()`** returns `{charter?: {text, updated}, notes: [{topic, text, updated}], size: {used, limit}}`. Notes are ordered by topic. `size` counts the notes other than the charter, against the 8,000-character total.

**`write_profile_note(topic, text)`**: creates or replaces the note for that topic. Blank `text` deletes it. Limits per section 6.2. Returns the same shape as `read_profile`.

### 8.19 `set_timezone` — Phase 5

`set_timezone(name)`: an IANA name such as `Europe/London`, stored in the database (a one-row `settings` table, added in Phase 5). The name is matched ignoring case (`europe/london` is stored as `Europe/London`). An unknown name is `invalid`, with the hint to use a name like `Europe/London` or `America/New_York`. Takes effect from the next call. Saving the zone already stored changes nothing and writes no event. `TASK_ORCHARD_TZ` in the environment still overrides it (section 3.1). Returns `{timezone, now}`, with `now` already in the new zone; when the environment overrides it, `now` stays in the environment's zone and a `note` says so. Called by onboarding once the user has confirmed the zone shown in `now`.

### 8.20 `get_guide` — done

Lists the guides, or returns one by name. Already implemented in `mcp/server.py`.

This is a tool of the MCP adapter only, for outside assistants that have no other way to read a guide. It is not an `Orchard` operation and is not in the catalogue: the app puts the guides in its system prompt (section 10.2) and does not offer it.

## 9. Skills

A skill is one markdown file in `src/task_orchard/skills/`, in the format the guide loader (`guides.py`) parses: a header with `name` and `description`, then the instructions. The app puts every guide in its system prompt (section 10.2). For outside assistants the same file is served as an MCP prompt and through `get_guide`. Every skill in the table below up to Phase 5 exists: `overview`, `triage`, `what-now`, `subdivide`, `complete-and-reflect`, `onboarding`, `plan-time-block`, `housekeeping` and `weekly-review`.

Rules for writing every skill:

- Address the agent directly. State the goal first, then numbered steps.
- Name the exact tools to call. Assume a small model: be explicit about order and about when to stop.
- State what to do without asking and what to ask about.
- Keep each under about 600 words. `overview` may run to 800, since it is the map of all the others.
- **Write for both settings.** In the app the guides and the profile are already in the prompt; in an outside assistant they are not. So a guide never says "call `read_profile`" outright, and never names `get_guide` at all, even conditionally: the app has no such tool, and Haiku tried to call it when the overview mentioned it. Outside assistants learn of `get_guide` from `INSTRUCTIONS`; the app's preamble says every guide is already in the prompt. It says "if the profile is not already in front of you, call `read_profile`", and names another guide as "the `subdivide` guide", leaving how it is reached to the setting.
- **Mention a tool that does not exist yet conditionally** ("if you have `snooze_task`"), so a small model never calls a tool that is not there.
- When a phase adds tools, update `overview.md` if a concept changed. The session-start routine lives in two places: the app's preamble (section 10.2) and `INSTRUCTIONS` in `mcp/server.py`. Change them only if that routine changes, and keep them in step.

**The guess-first rules.** The user should rarely be asked for a field. `overview` and `triage` state these five rules, and every guide that fills in a task's fields follows them:

1. **Guess first.** Fill every field from the title, the parent, matching patterns and the profile. Do not ask before guessing.
2. **Ask only when the answer is both unclear and consequential.** A missing deadline on something that sounds date-bound is worth a question. The drain of "buy milk" is not.
3. **Keep a question budget.** No questions at capture. At most one per task at triage, batched so several tasks are settled in one exchange. Larger questions wait for the weekly review.
4. **Prefer questions that generalise**, and save the answer to the pattern or the profile so it is never asked again.
5. **Say what was assumed, briefly,** when it matters. A correction from the user is cheaper than a question to them.

What each must contain:

| Skill | Phase | Required content |
|---|---|---|
| `triage` | 2 | Read the inbox (`search_tasks(inbox_only=true)`). For each task: guess every field from its wording, parent, `find_patterns` (from Phase 3) and the profile; write them with `update_task` and correct `sources`. The guess-first rules above, including the question budget: none at capture, at most one per task, batched across tasks. Spot recurring intent ("twice a week") and convert with `recurrence` (from Phase 4), after checking with `search_tasks` that no recurring task already covers it; for "the last day of the month" use `BYMONTHDAY=-1`. Spot duplicates from `possible_duplicates`. Spawned scheduled instances (rows with `instance_of`, from Phase 4) also land in the inbox: fill them like any task, using the parent and the previous occurrence (`get_task` on the parent shows its recent children), and do not ask the user about them. End by telling the user, in two or three lines, what was assumed. |
| `what-now` | 2 | Map the request to a tool: one task or a few options → `get_candidates`; "what's urgent" → `get_critical`. Pass `minutes_available` and `energy` only if the user said them or they would change the answer. Narrow with `under_id`, `min_priority` or `due_within_days` when the user asks about one project, only important things, or a time window, or when `pool.cut` shows much was left out. Rank using the facts, the profile and the charter. Offer 1 to 5 options, each with a one-line reason drawn from the row's facts. When giving several, spread them across motives and say which is which. For a high-activation pick, propose a smaller first step. If a pick has `achievable` of `no` or `unknown`, hand off to `subdivide`. A flexible recurring candidate (its row has `recurring`) is offered as itself ("go for a run?"); completing it records an occurrence. If `inbox` is above zero and the user has time, offer to triage first, since inbox tasks are never candidates. If the user declines an option as "not now", call `snooze_task`. When an offered task's soft deadline is long past, ask once whether to re-date it or clear it, so stale dates do not crowd the shortlist before housekeeping exists. |
| `subdivide` | 3 | Read the task with `get_task`. Break down **one layer only**, into 2 to 7 children that are each achievable or clearly need their own breakdown later. Use `sequential` or `after` only where order is real. Act alone when the breakdown is obvious; otherwise propose it and ask. Never plan the whole tree. Stop after two levels in one turn. Call `subdivide_task` once. |
| `complete-and-reflect` | 3 | Find the task first with `search_tasks(status="any")`; decide between existing, recurring and new (section 8.7). Ask one short question about how it went if the user did not say. Fill `actual_minutes`, `felt_activation`, `felt_drain` from their words. File a pattern: reuse when the match is clear, ask when not. For an occurrence of a recurring task, reuse the pattern its earlier occurrences were filed under; patterns are never assigned automatically. Handle `parent_check`: close or continue without asking when obvious, and say what was done. Handle `recurrence_check` (Phase 4). Update the pattern's `learned` note when the reflection teaches something general. |
| `onboarding` | 5 | Offered by `overview` when there is no charter. Explain the idea in four sentences. Draft the charter with the user and save it. Confirm the timezone shown in `now` and save it with `set_timezone`. Take a brain-dump with `capture_tasks`, then run `triage`. Ask about fixed obligations and habits and set up recurrence. Seed two or three profile notes. |
| `plan-time-block` | 5 | Take the block length and energy. Call `get_candidates` with them and `get_recent_activity` for the balance. Read the charter. Build an ordered plan that fits, with a buffer of about a fifth of the block, a high-activation item early, and a share for wants as the charter directs. Present it as a short list with times. |
| `housekeeping` | 5 | Call `get_review_queue`. Without asking: merge exact duplicates, clear soft deadlines long past on low-priority tasks when the deadline was not set by the user, consolidate profile notes that overlap. It never picks a new date. Never drop a task or change a `user`-sourced field without asking. Report what was done in a few lines and list what needs the user's decision. |
| `weekly-review` | 5 | With the user. Walk `get_review_queue` and `get_recent_activity(days=7)`: what was done, the balance against the charter, what keeps being deferred or restarted, missed recurrences, conflicts. For each stale item offer: drop, reshape, or recommit. Revisit the charter if the balance is far from it. |
| `morning-plan`, `evening-reflection` | 6 | For unattended runs of the app (section 12), where nobody is there to answer a question. Morning: `get_critical`, then a plan for the day. Evening: `get_recent_activity`, collect reflections on anything completed without one. |

## 10. Tools, the app and the MCP adapter

Three things sit above `core`. The **tool catalogue** says what a model may call. The **app** is Task Orchard's own assistant and the primary way in. The **MCP adapter** offers the same tools to other assistants. REST is in section 12.

```
the app (agent/)        MCP adapter (mcp/)       REST (api/, Phase 6)
       └──────────┬──────────┘                        │
          tool catalogue (tools.py)                   │
                  └──────────────┬────────────────────┘
                          Orchard (core/)
                                 │
                              SQLite
```

### 10.1 The tool catalogue — Phase 2A

`src/task_orchard/tools.py` is the one place a tool is defined. The app and the MCP adapter are both built from it, so a tool is described once and the two cannot drift.

- **One plain function per operation of section 8, with the same name.** A tool function does three things and nothing else: accept arguments, call one `Orchard` method, return its dict.
- **`catalogue(orchard) -> list[Tool]`**, in the order of section 8. The order is fixed, because the app sends the list to the model in this order on every request (section 10.2). A `Tool` has:

  | Field | Content |
  |---|---|
  | `name` | The operation's name. |
  | `description` | The function's docstring. |
  | `input_schema` | A JSON Schema object for the parameters, derived from the function's annotated signature with pydantic. The signature is the single source: nothing is written out twice. It has `additionalProperties: false`, titles dropped and `$defs` inlined, so the schema the app sends matches what FastMCP shows MCP clients. |
  | `read_only` | `true` for the operations that only read. The bookkeeping `roll_forward` does at the start of a read does not count against this. |
  | `fn` | The function. |

- **`call_tool(tools, name, arguments) -> Outcome`**, where `Outcome` is `{payload: dict, is_error: bool}`. This is the only way the app runs a tool. It never raises for a failure the model could cause:

  | Failure | Result |
  |---|---|
  | An `OrchardError` | `payload` is the error JSON of section 4.2; `is_error` is true. |
  | No tool of that name | `invalid`, with the hint listing the tool names. |
  | Arguments that do not fit the schema: a required one missing, a wrong type, a parameter that does not exist | `invalid`, with a hint naming the parameter and what it should be. Nothing is called. Validation is pydantic's lax mode, as in the MCP adapter: `"30"` is accepted for a whole number, `"lots"` is not. |

  Anything else is a bug and propagates.
- **Descriptions** are read by the model on every turn. Each says, in at most about 80 words: when to use the tool, in the user's terms; and anything surprising about it. Parameter descriptions give one example value each.
- **Parameters** use the wire forms of section 4: names for enumerated values, strings per section 3.2 for times. Declare an enumerated parameter as a string whose schema lists the names as an `enum`, so the model sees the choices; not as `Literal[...]`, which is validated strictly and would reject `"Must"` before `core` can match it leniently (section 13).
- `get_guide` is not in the catalogue (section 8.20).

### 10.2 The app — Phase 2A

`src/task_orchard/agent/`. It imports `core`, `tools`, `guides` and `config`. Only `cli` imports it.

The app is a loop around a model. It holds no task logic and makes no judgement in code: it builds the prompt, sends the conversation, runs the tools the model asks for, and repeats.

#### A turn

```
run_turn(session, user_text):
    append a user message: the "now" line, then user_text
    repeat at most MAX_STEPS times:
        reply = session.model.respond(system, tools, messages)
        append the reply's content to messages, unchanged
        if reply.stop is not "tool_use":
            if the reply holds any tool calls: append ONE user message answering each as not run
            return what the reply says (see below)
        run each tool call in the reply through call_tool, in the order given
        append ONE user message holding every result, each marked is_error when it failed
    return the step-limit text
```

- `MAX_STEPS = 12` model calls. It lives in `agent/loop.py` and nowhere else. When it is reached the loop makes no further call and the turn's answer is: `I stopped after 12 steps without finishing. Say "continue" to carry on, or ask in a different way.`
- **Tool calls run one at a time, in order.** Each is one `Orchard` call and so one transaction, as for any caller. A failed call does not stop the ones after it; the model sees every result and corrects itself from the hints.
- **All the results of one reply go back in a single message.** Splitting them teaches the model to stop asking for several things at once.
- **How a turn ends**, by `reply.stop`:

  | `stop` | The turn's answer |
  |---|---|
  | `end` | The reply's text. |
  | `max_tokens` | The reply's text, followed by `(The answer was cut off. Say "continue" for the rest.)` |
  | `refusal` | `The model declined to answer that.` |

  Any other value (`stop_sequence`, `pause_turn`, `model_context_window_exceeded`) is treated as `end`.
- **Every tool call gets a result.** The API rejects a conversation in which a tool call is not followed by its result, and the append-only rule below means the call cannot be removed. A reply cut off by `max_tokens` can end part-way through a tool call, so when a turn ends on a reply that still holds tool calls, the loop appends one user message answering each of them with `is_error` and the payload `{"error": "not_run", "message": "This call was cut off and did not run.", "hint": "Call it again if it is still needed."}`. Nothing is run. Without this, every later turn of the session would fail.
- **A user message may follow tool results.** When a turn ends at the step limit, or the model call fails, the conversation ends with a message of tool results, and the next turn's user message comes straight after it. The API joins consecutive user messages, so nothing special is done.
- **If the model call itself fails** (network, credentials, rate limit after the SDK's own retries), the error is reported to the user and the turn ends. Nothing already appended is removed, including the results of tools that ran: those changes happened, and the model must still see them on the next turn.

#### The session is append-only

Within a session the system prompt, the tool list and every earlier message are sent again exactly as they were. Nothing is edited, re-ordered or dropped.

This is a hard rule and not a style choice. Current models tie their reasoning to the exact conversation that produced it, and a request whose earlier part has changed can be rejected; the prompt cache is keyed on the same prefix. It has four consequences:

- **The profile is read once, when the session starts.** A `write_profile_note` during the session does not rebuild the prompt; the tool's result, which carries the whole profile, is how the model sees the change.
- **The time travels with each user message**, not in the system prompt. Every user message begins with one line, `[Now: Fri 2026-11-20T17:00, Europe/London]`: the weekday, the `at` form of section 3.3, and the zone. Tool results carry `now` as well (section 4.1).
- **The reply is passed back whole.** The loop reads the text and the tool calls out of a reply, but appends the reply's content exactly as received, including parts it does not understand.
- **A long session is ended, not trimmed.** When a reply reports more than `SESSION_TOKEN_LIMIT = 150_000` input tokens, `chat` tells the user once that the session is getting long and that `/new` starts a fresh one. Memory lives in the server (profile, patterns, the tasks themselves), so a new session loses little. There is no summarising or dropping of old turns in this phase. The API's server-side compaction is the expected next step: it summarises earlier turns into a block that the loop appends like any other, so the rule still holds. It is left to later work on the app (section 14).

#### The system prompt

Built once per session by `agent/prompt.py`, in this order:

1. **The preamble**, `agent/preamble.md`: fixed text that takes over the job of the MCP adapter's `INSTRUCTIONS`. It says what Task Orchard is and that the model makes the judgements; that the guides below are its instructions and the one for a job is to be followed; that the profile below was current when the conversation started; that ids come only from tool results; that times are used as given and never calculated; and that an error's hint says how to correct the call. When the timezone was neither detected nor stored with `set_timezone`, it ends with a warning that asks the model to confirm the zone with the user and save it. That text is `TIMEZONE_WARNING` in `config.py`, so the preamble and the MCP adapter's `INSTRUCTIONS` share it without importing each other.
2. **The `overview` guide.**
3. **Every other guide**, in name order, each under a heading `## Guide: <name>` followed by its description and body.
4. **The profile**: the result of `read_profile()` as JSON, under the heading `## The user's profile`. Its `now` is left out: it would be the session's start time, and the current time already comes with each user message.

Parts 1 to 3 are one block, identical for every session until a release changes a guide, and are marked for prompt caching. Part 4 is a second block after it. Nothing that varies between sessions may appear before part 4. The conversation is cached too (section 10.2, the model).

All guides go in, every time. They are short (section 9), and this removes both the tool call an outside assistant spends fetching one and the chance that it is skipped. If a small model proves to blur ten guides together, the first fix is to choose the guides for the session in code, not to bring back `get_guide`.

#### The tools offered

Every tool in the catalogue, in catalogue order, the same for the whole session. A guide written before a tool exists mentions it conditionally (section 9), so the model never calls a tool that is not there.

#### Two tiers

The app has two model settings:

| Tier | Setting | Default | Effort | Meant for |
|---|---|---|---|---|
| basic | `TASK_ORCHARD_MODEL_BASIC` | `claude-haiku-5-5` | `medium` | Everyday conversation: capturing, triage, what now, completing, and the unattended runs of Phase 6. |
| strategic | `TASK_ORCHARD_MODEL_STRATEGIC` | `claude-haiku-5-5` | `high` | Work that needs more thought: breaking down a vague project, planning a block of time, the weekly review, onboarding. |

- **Both default to Haiku 5.5**, so the whole stack runs on the small model out of the box. Either setting can name a larger model with no change to code.
- **Effort is fixed per tier** in `agent/model.py`, not a setting, so the tiers differ even when both name the same model.
- **The user chooses the tier; code never does.** Choosing from what was asked would be a judgement made in code. A session starts on basic. `chat --strategic` and `ask --strategic` start on strategic, and in `chat`, `/strategic` and `/basic` switch for the turns that follow.
- **A switch keeps the session.** Nothing already sent changes; the next request simply names the other model and effort. It costs one request that is not served from the cache, which belongs to one model and effort. If the model differs, the API also ignores the earlier reasoning blocks (it drops them, unbilled). Both are acceptable for an occasional, deliberate switch.
- Scenarios and unattended runs name their tier (below, and section 12). The "meant for" column was a first guess, tested by the scenarios of Phase 5: every skill's scenario passes on Haiku 5.5 at both efforts, and the strategic ones also pass on Sonnet 5.5, which judged a tight time block and the first step of a breakdown better (section 13, "Tiers after Phase 5"). The user chose to keep Haiku 5.5 as the strategic default.
- Letting the model ask for the strategic tier itself is left to later work on the app (section 14).

#### The model

`agent/model.py` defines the one seam through which a model is reached, so the loop can be tested with no network:

```python
class Model(Protocol):
    def respond(self, system: list[dict], tools: list[dict], messages: list[dict]) -> Reply: ...
```

`Reply` has `content` (the reply's blocks, opaque to the loop apart from text and tool calls), `stop` (`end`, `tool_use`, `max_tokens` or `refusal`) and `input_tokens` (everything the request sent, cached or not).

- **`AnthropicModel`** uses the official `anthropic` SDK. It is the only module that imports it.
  - One `AnthropicModel` per tier: the model id comes from the tier's setting, and the effort from the table above.
  - Credentials are whatever the SDK finds for itself (`ANTHROPIC_API_KEY`, or a login profile). The app never reads, stores or prints a key.
  - One request per `respond`, not streamed, with `max_tokens` of 16,000.
  - **Caching.** Besides the marker on the stable block of the system prompt, the request uses the API's automatic caching, so the conversation so far is cached for the next call. A turn of several tool calls then pays full price only for what each call added. Each model has a minimum length below which nothing is cached; the first scenario run confirmed from the usage figures that the cache is read on Haiku 5.5.
  - **Refusals.** Where the model supports a server-side fallback on a refusal, it is turned on. The models that do are a fixed list, `FALLBACK_MODELS` in `agent/model.py` (Opus 5.5 and 5, Sonnet 5.5, Fable 5.2 and 5.1), which needs updating when models change. Haiku 5.5 does not, so on the default settings a refusal ends the turn as above.
  - The request's exact shape (thinking, effort, cache markers, fallback) follows the current Claude API documentation. Check it before writing; do not work from memory. Whatever it needs stays inside this module.
- **`ScriptedModel`**, in `tests/`, returns replies from a list and records what it was sent. Every test of the app uses it. The test suite never calls a model.

#### `task-orchard chat` and `task-orchard ask`

- **`task-orchard chat`** is a terminal conversation. Each line typed is a turn. The answer goes to stdout. One line per tool call goes to stderr as it happens, so the user can see what the model is doing: `-> get_candidates(minutes_available=30, energy="low")`, followed by ` !! <error code>` when the call failed.
  - `/new` ends the session and starts another, which reads the profile again and starts on basic. `/strategic` and `/basic` switch tier. `/exit` or `/quit`, or end of input, exits. An unknown `/` command is answered with the list of commands.
  - At a terminal, input is read through Python's `readline` module, which gives line editing (arrows, Ctrl-A and Ctrl-E) and history; up to 1,000 typed lines are kept across runs in `$XDG_STATE_HOME/task-orchard/chat_history`. When input or output is not a terminal, lines are read plainly.
  - The tier and model in use are printed to stderr when a session starts and on each switch, as `[basic: claude-haiku-5-5]`.
  - A transcript is kept by default: every message and tool outcome, as JSON lines, each tagged with the session's number, since `/new` starts another in the same file. It goes to a new file, `$XDG_CACHE_HOME/task-orchard/transcripts/chat-<UTC time>.jsonl`, unless `--transcript PATH` names a file to append to. `--no-transcript` keeps none. The path is printed to stderr at the start.
- **`task-orchard ask "<text>"`** runs one turn in a new session, prints the answer and exits: 0 on success, 1 if the model could not be reached.
- With no usable credentials, both print how to supply them (set `ANTHROPIC_API_KEY`, or log in with the `ant` CLI) to stderr and exit 1, before asking for any input or opening the database. The SDK builds a client without credentials and fails only at the first request, so both commands ask the SDK's own resolution whether it found a key, a token or a login profile.
- Both open the database directly, as the MCP server does. No service needs to be running.
- **`task-orchard mcp`** is the explicit name for the stdio MCP server. The bare command and `serve` keep meaning the same thing until Phase 6, so existing client configurations go on working.

#### Scenarios

A scenario checks that a real model, given the app's prompt and tools, does the right thing. It is the automated form of the shakedown table in `README.md`.

- `tests/scenarios/`, one file per scenario: a database seeded by `seed_demo` at a fixed clock, one or more user messages, and what must then be true.
- **Assert on what was called and on the end state, never on wording**: which tools were called and with which arguments that matter, which tools were not called, and what `Orchard` reads return afterwards.
- They are pytest tests marked `scenario`. The default run excludes them (`-m "not scenario"` in `addopts`), and they skip when there are no credentials. Run them with `uv run pytest -m scenario`. They cost money and are never part of a phase's acceptance.
- Each scenario names its tier and runs on that tier's setting, so by default on Haiku 5.5: ground rule 2 says the tools and guides must work on a small model, and this is where that is checked. To see whether a larger model does better, set the tier's variable for the run.
- Phase 2A adds two: "I've got 30 minutes and I'm tired" (expects `get_candidates` with `minutes_available=30` and `energy="low"`, and no write), and "Remind me to buy stamps, call Mum and fix the bike light" (expects one `capture_tasks` call with three items, and three more tasks in the inbox). Each later phase adds one per new skill.

### 10.3 The MCP adapter

`src/task_orchard/mcp/`. It exists, with the Phase 1 and 2 tools over stdio, and stays working. It is not where behaviour is designed or tested (ground rule 7), and no phase before 6 does MCP-specific work beyond what is listed here.

- **It is generic.** `build_server` registers every tool in the catalogue with FastMCP: the same function, so the same name, description and schema, with `read_only` passed as `annotations={"readOnlyHint": True}`. Each tool is wrapped so that an `OrchardError` becomes a tool error whose text is the JSON of section 4.2. `tests/test_scaffold.py` shows the client pattern that works with the installed FastMCP.
- It therefore gains each later phase's tools with no further work. There are no per-phase MCP test modules after Phase 2; one module, `test_mcp_adapter.py`, proves the wiring (section 11).
- `get_guide`, the MCP prompts (one per skill) and `INSTRUCTIONS` stay as they are. `INSTRUCTIONS` tells the client to read the profile with `read_profile` at the start of a conversation, then the `overview` guide.
- Transport is stdio through Phase 5. Remember that stdout belongs to the protocol.
- **What waits for Phase 6:** HTTP transport and a token, a review of the tool set and the responses for assistants that will not read the guides, and the check of how a given MCP host surfaces prompts and instructions.
- **Several processes, one file.** Before Phase 6, the app and each MCP client are separate processes on one SQLite file. A deferred transaction that reads and then writes can fail with "database is locked" in WAL mode, and `busy_timeout` does not help in that case. So every transaction starts with `BEGIN IMMEDIATE`, which takes the write lock up front and waits for it under `busy_timeout`. `roll_forward` may write on any call, so read calls take the lock too; with one user this costs nothing.

## 11. Phases and acceptance

Work one phase at a time. A phase is done when its listed tests pass, `ruff check` is clean, and every earlier test still passes. Write the tests named here first.

The order is 0, 1, 2, 2A, 3, 4, 5, 6. Phase 2A was added after Phases 3 to 6 had been specified and is named so that their numbers, which this document and the code refer to throughout, did not have to change.

Test at two levels: the service directly (`orchard.method(...)`), and each tool once through the catalogue's `call_tool` (section 10.1) to prove wiring, schema and error shape. Phases 1 and 2 did the second through the in-memory MCP client, in `test_mcp_phase1.py` and `test_mcp_phase2.py`; those modules stay and must keep passing. The app is tested with `ScriptedModel` (section 10.2). No test in the normal run calls a model or the network. Use the `orchard`, `clock` and `seeded` fixtures.

From Phase 3 on, every phase also does these three things, which are not repeated under each one:

- its operations are added to the catalogue, which gives them to the app and to the MCP adapter with no further work;
- its skills are added to `skills/`, and so to the app's prompt, written for both settings (section 9);
- it adds one scenario per new skill (section 10.2). Before the phase is called done, ask the user for the go-ahead to run them, since they are billed, then run them and report the result. Scenarios are not part of acceptance, because they cost money and a model's behaviour varies.

### Phase 0 — Scaffold and schema (done)

Project, schema, migrations, clock, config, events helper, transaction wrapper, demo seed, MCP shell with `get_guide`. Tests: `tests/test_schema.py`, `tests/test_scaffold.py`.

### Phase 1 — Tasks and graph (done)

Build: `timefmt`, `views`, `graph`, the predicates of `derive`, and `capture_tasks`, `create_task` (without recurrence), `update_task` (without recurrence), `link_tasks`, `unlink_tasks`, `get_task`, `get_tree`, `search_tasks`.

Some row keys arrive with the phase that computes them: `deferred`, `starts`, `touched` and `pattern` in Phase 3; the computed keys of the `recurring` block in Phase 4 (rows already carry its stored part: `mode`, `cadence`, `gap_days`); `pattern.ratio` in Phase 5. Until the digest arrives in Phase 3, the detail shows `pattern` as `{id, name}`.

The seed already contains recurring tasks, so the predicates must handle them from Phase 1. Until Phase 4:

- a write whose target is a recurring task raises `unsupported`;
- so does creating a task under a recurring parent, because in Phase 4 that creates an occurrence under the one-open-instance rule, and a plain child made earlier would leave data that breaks it;
- a recurring task as a blocker is rejected from Phase 1 (`unsupported`, section 7.3), because that rule is permanent and needs nothing from Phase 4; being blocked is allowed;
- reads treat a recurring task like any other task, and its instances behave as plain tasks.

Every transaction begins with `BEGIN IMMEDIATE` from Phase 1 (section 10.3).

| Test module | Must prove |
|---|---|
| `test_timefmt.py` | Every form in 3.2, including what a bare day means for each kind of parameter, weekday-after-today, and both daylight-saving edge cases; day-counted snoozes ending at local midnight (3.4); every row of the `rel` table; rejection with the stated hint. |
| `test_graph.py` | Each rejection case in 5.2 with a readable path that includes each step's `status`. A loop through a closed task is rejected, and unlinking that closed link then allows the change. Property test (hypothesis): after any sequence of accepted links and moves, the graph is acyclic. |
| `test_derive_predicates.py` | Each predicate, including dormancy and blocking inherited from an ancestor. Property test: no candidate is ever blocked, dormant, untriaged or a parent of open children. Against the seed: `offsite_venue` is blocked through `offsite`; `taxes` is dormant; `release_notes` is blocked; `cert` is a candidate. |
| `test_tasks_crud.py` | Create, update, clear, move; field sources and protection (4.5), including a protected `clear` and the top-level `hint` whenever something is skipped; `firmness` alone and `deadline` alone on update; closed-task restrictions; adding a child flips the parent's `achievable`, and `achievable: "yes"` is refused on a task with open or done children but allowed when they are all dropped; lenient enum names (`"Must"`, `" high "`); `clear` takes `parent_id` and `pattern_id`; the Phase 1 recurring restrictions; notes length limit; exact events per 6.4, including `source` in `updated` payloads; a failed call writes nothing. |
| `test_read_tools.py` | Row and detail shapes, omission of empty keys, truncation (80 for row titles, 40 inside nested references), `path` cut to three ancestors, tree limits, search including quoting of hostile input such as `"`, `*` and `NEAR`, and `invalid` for a query with no words. |
| `test_mcp_phase1.py` | Each tool callable through the client; an error arrives as JSON with `error` and `hint`. |
| `test_concurrency.py` | Two `Orchard` instances on one file, writing alternately and from two threads at once, never raise "database is locked". |

### Phase 2 — Prioritisation (done)

Build: effective values and section 5.5 in `derive`, `shortlist`, `get_candidates`, `get_critical`, `read_profile`, `write_profile_note`. Skills: `triage`, `what-now`.

| Test module | Must prove |
|---|---|
| `test_effective.py` | Each rule in 5.4. Against the seed: `cert` has effective priority `critical` via `release_publish`, and a hard `due` 20 minutes (that task's estimate) before the release deadline; `game_build` has motive `want` via `game`. Unlinking `cert` restores `low` and no deadline. `hard_due` appears only when later than `due`. Property test: effective priority is never below own priority. |
| `test_shortlist.py` | Grouping and order per 5.7; each of the five filters alone and combined, its count, and first-filter-wins counting; `under_id` includes the task itself and rejects an unknown id; `over_energy` never filters; reserved wants are brought in, marked, and replace only non-want rows from `other`; the reserve is `min(reserve_wants, limit // 3)`, so `limit=1` returns the top of the order; `pool` counts add up. Against the seed: `release_flaky` and `cert` are `pressing`; `summary_w3` is present and overdue; `novel` or a `game_*` task is present even at `limit=5`. |
| `test_critical.py` | Blocked, dormant and inbox leaves are included, blocked ones with `blocked_by` (at most 5 plus `blocked_by_more`); non-leaves and recurring tasks are not; horizon boundaries. At risk is judged on the work of whoever owns the deadline (5.5): the leaves of a project due in three days with thirty hours of open work are listed, while a leaf with its own earlier deadline is judged on its own estimate. |
| `test_profile.py` | Upsert, delete by blank text, case-insensitive topics, each limit and `profile_full`. |

### Phase 2A — The app (done)

Build: the tool catalogue (section 10.1), the app (section 10.2) with `task-orchard chat` and `task-orchard ask`, the generic MCP adapter (section 10.3), and the scenario harness with its first two scenarios.

This phase added no operation and changed no behaviour of `core`. There was no schema change, so no migration, and existing databases were untouched.

The tool functions moved out of `mcp/server.py` into the catalogue unchanged, and the guide loader moved up from `mcp/`. `test_mcp_phase1.py` and `test_mcp_phase2.py` passed with no edit beyond one import line, which is the proof that nothing an MCP client sees changed. Both scenarios pass on Haiku 5.5, with the prompt served from the cache.

| Test module | Must prove |
|---|---|
| `test_tools.py` | The catalogue has one tool for every operation built so far, named as its `Orchard` method, in the order of section 8. Every schema is an object whose parameters each have a description; enumerated parameters list their names and still accept `"Must"`; `read_only` is set for exactly the read operations. `call_tool` returns the same dict as the `Orchard` method on success; turns an `OrchardError` into the JSON of section 4.2 with `is_error`; and answers an unknown tool, a missing required argument, a wrong type and an unknown parameter with `invalid` and a hint, having written nothing. |
| `test_agent_prompt.py` | The four parts in order, the overview first and every other guide present under its heading. The profile block holds the charter and notes as they were when the session started. The block before it is identical for two sessions with different profiles. The timezone warning appears only when the zone was neither detected nor stored. Every user message starts with the `[Now: …]` line, built from the clock. |
| `test_agent_loop.py` | With `ScriptedModel`: a reply with no tool call ends the turn with its text. A tool call changes the database and its result goes back. Two calls in one reply run in order and their results go back in one message. An `OrchardError` goes back as a failed result and the loop continues, so a scripted retry succeeds. The step limit, `max_tokens` and `refusal` each end the turn with the stated text. A reply cut off with a tool call in it gets a `not_run` result for that call, nothing runs, and in the next request every tool call is followed by its result. Over several turns, the system prompt, the tool list and every earlier message of each request equal those of the request before it. A reply's content is passed back unchanged, including a block the loop does not know. A model that raises leaves the conversation as it was, including tool results already appended. |
| `test_chat_cli.py` | With a scripted model injected: `ask` prints the answer and exits 0, and exits 1 when the model cannot be reached. `chat` answers on stdout and reports tool calls on stderr; `/new` starts a session that reads the profile again; `/exit`, `/quit` and end of input exit; the transcript writes one JSON line per message and tool outcome, goes to the cache directory unless `--transcript` names a file, is off with `--no-transcript`, and its path is printed; the long-session notice appears once. A session starts on the basic tier's model; `--strategic`, `/strategic` and `/basic` select the other tier's model and effort, and a switch keeps every earlier message. Missing credentials give the stated message and exit 1 before any input is read. |
| `test_mcp_adapter.py` | Through the in-memory client: the server lists exactly the catalogue's tools plus `get_guide`, with the catalogue's descriptions; one read, one write and one error round-trip. |
| `test_agent_model.py` | Offline, with a fake client: the tiers come from the settings with their efforts; the request marks the stable system block and the conversation for caching and sends no thinking setting; fallback is requested only for models that support it; a reply's blocks are read whole; stop reasons map as stated; an API failure becomes `ModelUnavailable`. |
| `test_layering.py` | By reading the imports of every module: `core` imports nothing from `tools`, `agent`, `mcp` or `api`; `tools` imports nothing from `agent`, `mcp` or `api`; `agent` and `mcp` import nothing from each other; only `agent/model.py` imports `anthropic`. |

### Phase 3 — Leading edge and completion (done)

Build: `subdivide_task`, `start_task`, `complete_task` (without recurrence), `snooze_task`, `drop_tasks`, `reopen_task`, per-task counts (5.6), `find_patterns`, `save_pattern` (stored fields and counts). Skills: `subdivide`, `complete-and-reflect`.

Until Phase 4, the Phase 1 rule for recurring tasks held, in its simplest form: `start_task`, `complete_task`, `snooze_task`, `drop_tasks` and `reopen_task` raise `unsupported` when the target is a recurring task, and `reopen_task` also when it is an instance (the instance cap arrives with recurrence). Phase 4 adds every recurrence path at once. An open instance is otherwise a plain task: it can be completed, snoozed or dropped, with no `next_slot` or `recurrence_check`. No schema change was needed.

Done. The scenarios `subdivide` (strategic tier) and `complete-and-reflect` (basic) pass on Haiku 5.5, as do the two from Phase 2A.

| Test module | Must prove |
|---|---|
| `test_subdivide.py` | Atomicity (one bad child creates none); `sequential`; `after` by position; cycle rejection; events. |
| `test_complete.py` | Completion row and snapshots (effective motive at the time, and the estimate's source); past `completed_at`; future rejected; untracked creation, including under a parent, which flips the parent's `achievable`; `has_open_children` and `drop_open_children`; `parent_check` appears exactly when specified; `unblocked`. A parent left with no open children then appears in `get_candidates` with `achievable: "no"`. |
| `test_lifecycle.py` | Snooze, wake, `days: 0`, fractional `days` rejected; drop cascade, `skipped` and `unblocked`; `last_touched` moves for each touch event and for a `user`-sourced update, but not for a `guess` update, a housekeeping edit, or `roll_forward`; reopen rules and restoration of a deleted completion's data in the event; counts in rows (`deferred`, `starts`); `update_task(snooze_until=)` does not count as a deferral. |
| `test_patterns.py` | Create, rename, name clash, `learned` limit, merge moves tasks, search. |
| `test_events_exhaustive.py` | For every mutating operation: exactly the events of 6.4, in the same transaction. |

### Phase 4 — Recurrence (done)

Build: `core/recurrence.py`, the `recurrence` object in `create_task` and `update_task`, recurrence paths in `complete_task`, `snooze_task`, `drop_tasks`. Change the seed so scheduled instances are produced by `roll_forward` where practical.

| Test module | Must prove |
|---|---|
| `test_rrule.py` | Validation rules, including rejection of `BYHOUR`, `BYMINUTE` and `BYSECOND`, and acceptance of `UNTIL` with `Z`; `next_slot` for weekly, monthly, month-end (`BYMONTHDAY=-1`) and yearly rules; 17:00 local stays 17:00 across the 29 March 2026 change in `Europe/London`. |
| `test_roll_forward.py` | First instance appears when the lead window opens, untriaged, with only title, parent, slot and deadline set, and is listed by `get_critical`; idempotence; a missed instance's open descendants are dropped; a rule that runs out retires its task once no instance is open; lockstep whether the previous instance was done early, late or never; the two-instance cap and `missed`; a long absence leaves two open and the rest missed; pause and resume skip slots without marking them missed; the 60-per-request bound continues on the next request. |
| `test_recurring_complete.py` | Completing a scheduled task directly, with and without an open instance; flexible completion directly and through an instance; `gap_days` snooze; the recurrence check's contents; `instance_exists`; creating an instance copies only the title and wakes a sleeping flexible task; flexible task reappears as a candidate when awake with `past_wake_days`. |
| `test_recurring_rules.py` | Every rule in 7.3, including that a recurring task has no `achievable` and gets no pattern unless one is given; conversion from a plain task. |

The seed's recurring history stays hand-built, because replaying four weeks through `roll_forward` would mean running the seed against a moving clock; `test_seed_roll.py` checks that its rows and events are what `roll_forward` would have written, and that a request on the seed writes nothing. `tests/scenarios/test_recurring.py` checks setting up a monthly scheduled task and recording a run against the flexible task.

### Phase 5 — Learning and review (done)

Build: full pattern digests, the balance, `get_review_queue`, `get_recent_activity`, `merge_tasks`, the `settings` table and `set_timezone`. Skills: `onboarding`, `plan-time-block`, `housekeeping`, `weekly-review`. Complete the skill scenarios.

| Test module | Must prove |
|---|---|
| `test_digest.py` | Every statistic in 6.1 on hand-built data, including medians on even counts, the 20-completion window, exclusion of recurring containers, and exclusion of `pattern` and `evidence` estimates from `ratio`. Against the seed: "Interface specs" has a `ratio` above 2. |
| `test_balance.py` | Windows, grouping, the `estimated` and `unknown_duration` rules. |
| `test_review_queue.py` | Each section's membership, order and limit. Against the seed: `dentist` leads `most_deferred` and is in `repeat_starts`; `milk` is in `overdue`; the weekly summary is in `missed`. A recurring task and its instances are never reported as duplicates. Items are slim rows. |
| `test_merge.py` | Children, links, fields, `pattern_id`, notes; refusal cases; cycle check. |
| `test_settings.py` | `set_timezone` stores the zone and the next call uses it; an unknown name is `invalid`; `TASK_ORCHARD_TZ` still wins. |
| `test_response_size.py` | With 500 generated tasks, every read tool's response stays under 20,000 characters. A list of worst-case rows stays under 30,000 (section 13, "Worst-case rows"). |

**Skill scenarios.** The harness and the first scenarios exist from Phase 2A, and each phase since has added one per skill (section 10.2). By the end of this phase every skill has at least one. Run the whole set on the defaults (both tiers on Haiku 5.5), then with the strategic tier on a larger model, and report to the user which skills the small model handles well enough. That answers which tier each skill belongs in, and whether the strategic default should change.

Also revisit, with the user, whether to log suggestions that were offered and not chosen. Revisited: the user is watching for the need, and the question now lives in section 14.

**As built.** The tests above pass, and `test_mcp_phase2.py` checks that every guide ships and stays within its length. The scenarios are in `tests/scenarios/test_phase_5.py` (onboarding, plan-time-block, housekeeping, weekly review, and triage, which had none). All eleven pass on the defaults; the strategic four were also run on Sonnet 5.5 (section 13, "Tiers after Phase 5"). Logging offered suggestions is not built (section 14).

### Phase 6

Section 12.

## 12. Phase 6: the service

To be detailed before it starts; the shape is fixed. Through Phase 5 Task Orchard is a set of commands that each open the database. Phase 6 makes it one long-running process that everything else connects to, and builds out the two adapters that were set aside while the app came first: REST for programs and MCP for other assistants.

- **One process, one writer.** `task-orchard serve` runs an HTTP service on `TASK_ORCHARD_HOST` and `TASK_ORCHARD_PORT`, with bearer-token auth from `TASK_ORCHARD_TOKEN`. It refuses to start on a non-loopback address without a token. SQLite is in WAL mode and all access goes through this process. This changes what `serve` and the bare command do today (they run the stdio MCP server). The user confirmed the layout: `serve` is the HTTP service, `mcp` is the stdio MCP server, and the bare command prints the help and exits non-zero, so a client configuration that still runs `task-orchard` or `task-orchard serve` fails with a clear message rather than starting the wrong thing. The fix is to change it to `task-orchard mcp`; `README.md` says so.
- **REST.** A FastAPI app in `src/task_orchard/api/`. Read routes only at first, each calling the same `Orchard` method as its tool: `GET /candidates`, `/critical`, `/tasks/{id}`, `/tree`, `/search`, `/review`, `/activity`, `/patterns`, `/profile`. Writes are added when the Android app needs them.
- **REST answers are for programs, not for a model.** The rows and details of section 4 are shaped for a model's context: titles are cut, empty keys vanish, times are local with a relative phrase, and lists stop at 30. A client that draws a screen needs the opposite. So the REST routes get their own view builders beside `core/views.py`: full titles, instants as ISO 8601 with an offset, every key always present (null when unset), and paging on lists. The facts and their derivation are the same; only the presentation differs. How `Orchard` returns both forms without doing the work twice is the main design question for this phase.
- **MCP for other assistants.** The adapter of section 10.3, served over FastMCP's HTTP transport from the same process, with the same token; stdio stays available as `task-orchard mcp`. Before it is offered more widely:
  - review the tool count for a host that shares its context with other servers, and merge tools that are always used together (plan section 10);
  - make sure each response carries the next-step hint an assistant needs when it has not read the guide, as `skipped` and `recurrence_check` already do;
  - confirm in the user's MCP host that tools are callable, and note in `README.md` whether it surfaces MCP prompts and server instructions.
- **The app over HTTP.** A chat endpoint that runs the loop of section 10.2 for a remote client, which is what the Android app's conversation needs. It requires conversations to be stored between requests, and so a schema change.
- **Unattended runs.** `task-orchard run <skill>` runs the app for one turn with nobody present, for `housekeeping`, `morning-plan` and `evening-reflection`. It uses the basic tier, or the strategic tier with `--strategic`. The run's final answer is stored as a report (a new table) and shown at the start of the next session. Task Orchard still starts nothing by itself: the operating system's scheduler (cron or a systemd timer) runs the command, and the deployment notes say how. Getting a report to the user without their opening a session is Phase 7.
- **Agent platforms.** Hermes, OpenClaw or similar can connect over MCP and use their own scheduling in place of the unattended runs above. Deployment notes for one are written only if the user decides to run it.
- **Backups.** Document a nightly `sqlite3 .backup` of the database file.

The Android application (Phase 7, sketched in plan section 10) is outside this specification.

## 13. Refinements over the plan

Decisions made while specifying and building, which go beyond `IMPLEMENTATION_PLAN.md` or replace what an earlier draft of it said. The rules themselves are stated in the sections they belong to; this table records what was decided and why.

| Topic | Refinement |
|---|---|
| Time input | A small fixed grammar (3.2) with weekday names and offsets, so the model does no date arithmetic. |
| Deadlines | Two effective deadlines, `due` and `hard_due`, so a soft early date does not hide a later hard one. |
| Shortlist | Three plain groups (`pressing`, `due_soon`, `other`) in place of a single "at risk first" rule; three named constants. Three optional narrowing filters (`under_id`, `min_priority`, `due_within_days`) under the agent's control. |
| Posture | "Facts in the server, judgement in the agent" is a default with a stated test for server-side heuristics (section 1), not a prohibition. |
| Deferral vs. scheduling | `snooze_task` counts as a deferral; `update_task(snooze_until=)` does not. This keeps the resistance signal clean when the agent sets a recurring task's return time. |
| Flexible instances | A flexible task has at most one open instance. Creating one under a sleeping flexible task wakes the task, so the new instance is not hidden by its parent's snooze. |
| Early completion | Completing a scheduled task before its instance exists consumes the next slot. |
| Rule frequency | Scheduled rules are daily at most. |
| Capture | `capture_tasks` takes several items, for brain-dumps. |
| `update_task` | Unsetting is explicit, through `clear`, because JSON cannot distinguish "omitted" from "null" reliably across clients. |
| Completion row | Stores the effective motive at completion, so the balance survives re-parenting. |
| Closed status | `closed_at` records when the task was closed in the system; `completed_at` lives on the completion row, records when the work was done, and may be earlier. |
| At risk | Judged against the remaining work of the whole project that owns the deadline, not the single task's estimate, so a large project is flagged before its last day. |
| Unset priority | A task with no priority of its own or inherited shows none, and is ordered as `normal`. |
| Conflicts | Evidence that disagrees with a user-stated value is logged as a `conflict` event and surfaced in the review queue. |
| Timezone | Read from the environment or the system, and from Phase 5 also from the database, where `set_timezone` stores it once the user confirms it. Precedence is in section 3.1. |
| Event types | Not constrained in the database, so new types need no migration. |
| No `kind` field | An early draft's `kind` field (`task` or `recurring`) is gone. It duplicated `recur_mode`, which was added later; a task is recurring exactly when it has a mode. |
| `must` | The third motive is `must`, where early drafts had `have_to`: `want`, `should`, `must` read as who is asking (the user, the user's own standard, someone else). The meaning is unchanged; a work project the user enjoys is still a `must`. |
| Instances | A recurring task is a container. Its instances are ordinary new tasks: only the title is copied, priority and motive arrive by inheritance, and the rest is filled in the way any new task's fields are. Spawned scheduled instances wait in the inbox for triage, and `get_critical` includes inbox leaves so a deadline cannot go unseen. An earlier draft had instances copy the recurring task's own fields. |
| Achievable | A task with open or done children is never achievable. Adding a child by any route flips the parent to `no`, and setting `yes` on such a task is refused. Recurring tasks have no achievable value at all. |
| Patterns | References only. Nothing is filed under a pattern automatically (an earlier draft gave every recurring task one), and nothing in the server derives a task's values from a pattern; the agent reads digests and decides. |
| Touched | Counts only events that show the user engaging with the task, and updates written with source `user`. Agent edits made on its own, and everything the clock does, are not touches, so tidying cannot hide staleness. |
| Protection | `clear` is protected like a write, and any skip adds a top-level `hint` telling the agent how to apply a change the user asked for. |
| Deadline and firmness | On update, `firmness` alone re-grades the existing deadline, and `deadline` alone keeps the existing firmness. |
| Names | Id parameters end in `_id`, and `clear` uses the same names. Enum names are matched ignoring case, whitespace, spaces and hyphens. |
| Cycle paths | Include each step's `status`, since loops may run through closed tasks; `unlink_tasks` works on closed tasks. |
| Response budget | Nested titles are cut to 40 characters, `path` to three ancestors, list limits to 30, `get_critical` blockers to 5, and review-queue items are slim rows. |
| Recurrence rules | `BYHOUR`, `BYMINUTE` and `BYSECOND` are rejected; a UTC `UNTIL` is converted to local time; a rule that runs out retires its task. A missed instance takes its open descendants with it. |
| Small additions | `merge_tasks` copies `pattern_id`; `drop_tasks` returns `unblocked`; `notes` is capped at 20,000 characters; a search with no words is `invalid`. |
| Transactions | Every transaction starts with `BEGIN IMMEDIATE`, so two clients on one file wait for each other instead of failing. Foreseeable failures are converted to `OrchardError` in `core`. |
| Estimate ratio | Completions record where the estimate came from (`estimate_source_at_completion`). The pattern `ratio` uses only `guess` and `user` estimates, because estimates derived from past completions would feed the ratio back into itself. |
| Enum names over MCP | Tool parameters for enumerated values are plain strings whose schema lists the names as an `enum`, not `Literal[...]`. FastMCP validates a `Literal` strictly and would reject `"Must"` before `core` sees it, defeating the lenient matching of 4.1. The model still sees the choices. |
| Duplicates | A title is normalised by case-folding, collapsing whitespace and removing punctuation at either end. Items earlier in the same `capture_tasks` call count as existing tasks, so a brain-dump that repeats itself is flagged. Pairs within one recurring family are not duplicates. |
| BEGIN IMMEDIATE | Python's `sqlite3` module runs a SELECT outside any transaction and issues its own `BEGIN` only before the first write, so a call's reads were never isolated from its writes. The engine turns that off and issues `BEGIN IMMEDIATE` itself; `test_concurrency.py` checks that a call holds the write lock from its first read. |
| App first | The plan put the AI in an MCP client and made MCP the way in. In use, the design turned out to describe a whole assistant (a session-start routine, guides to follow, a charter that governs, a model sized to the job, unattended upkeep) and then to depend on a host it does not control to honour it. So Task Orchard now has its own loop, the app, and behaviour is designed and tested there. MCP and REST are adapters over the same operations, built out in Phase 6. Nothing in `core` changed. |
| Phase 2A | The app is built before Phase 3, so that completing, snoozing and breaking down are first used through it. It is numbered 2A so that Phases 3 to 6 keep the numbers this document and the code already use. |
| Tool catalogue | Tools are defined once, in `tools.py`, as plain functions with annotated signatures. The app and the MCP adapter are both generated from it. Before Phase 2A they were defined inside the MCP server. |
| MCP adapter | Generic from Phase 2A: it registers whatever the catalogue holds, so it keeps up with later phases at no cost, and is checked by one test module. Everything specific to MCP (HTTP, a token, tuning for hosts that skip the guides, the check of what a given MCP host surfaces) waits for Phase 6, beside REST. |
| Guides in the prompt | The app puts every guide and the profile in its system prompt, in place of `get_guide` and the session-start `read_profile`. Guides are worded to hold in both settings. |
| Append-only sessions | Within a session the app never edits, re-orders or drops what it has already sent. So the profile is read once per session, the time is carried in each user message and not in the system prompt, and a long session is ended with `/new`, not trimmed or summarised. |
| Step limit | A turn is at most 12 model calls. This bounds one turn only; the user can say "continue". |
| Models | Two tiers, basic and strategic, each a setting that names a model, both defaulting to `claude-haiku-5-5`. Effort is fixed per tier (`medium`, `high`). The user picks the tier and code never does. This makes ground rule 2 true of the whole stack, not just of a test, and leaves a larger model one setting away for the work that needs it. Scenarios and unattended runs use the same two settings, so there are no others. |
| Unanswered tool calls | A turn that ends on a reply still holding tool calls (cut off, or refused) answers each with a `not_run` result. The API rejects a tool call with no result, and the append-only rule forbids removing it, so without this one cut-off reply would break the rest of the session. |
| Caching | The stable part of the system prompt and the conversation so far are both cached, the second with the API's automatic caching. Every model call in a turn re-sends the whole conversation, so caching only the system prompt would leave most of the cost in place. |
| Compaction and the agent side | Not used in Phase 2A: a long session ends with `/new`. Server-side compaction fits the append-only rule (the summary arrives as a block the loop appends like any other) and is the expected next step. It belongs with the wider work on how the app manages the model's context and choices, which is expected to grow: compaction, choosing guides per session, letting the model ask for the strategic tier. |
| Model seam | The loop reaches a model only through the `Model` protocol. One module imports the vendor SDK, and tests use a scripted model. The loop is written by hand, not taken from an SDK helper, so that the append-only rule, the step limit and the transcript are in our code and testable. |
| Scenarios | Run through the app's loop by `pytest -m scenario`, from Phase 2A, in place of manual scripts run by hand in an MCP host. They assert on tool calls and end state, are excluded from the normal run, and are never part of acceptance. |
| REST views | REST routes do not return the model-facing rows of section 4, as first planned. They get program-facing views: full titles, instants with offsets, stable keys and paging. |
| Unattended runs | In Phase 6, housekeeping and the morning and evening skills are runs of the app started by the operating system's scheduler. Task Orchard still has no background process of its own. An agent platform over MCP remains possible but is no longer the plan. |
| Phase 3 and recurrence | Every write on a recurring target stayed `unsupported` until Phase 4, including starting and snoozing a flexible task, which section 8.6 and 7.2 would allow on their own. A half-built recurrence path is more work than it saves while the phases move quickly; Phase 4 added them together. |
| Achievable flip in subdivide | `subdivide_task` logs the parent's flip to `achievable: no` with `updated`, like every other way of adding a child. The first draft of 6.4 left it out for this one route. |
| Pattern at completion | The `completed` event carries `pattern_id` when the call filed the task, so filing at completion is logged like any other change (ground rule 5). |
| `unblocked` | Includes descendants freed through a blocked parent (the venue as well as the offsite), and is capped at 10 rows with `unblocked_more`, so a large project cannot blow the response budget. |
| `drop_tasks` | `parent_checks` reuses `complete_task`'s `parent_check` shape as a list. A listed task is reported in `dropped` even when another listed task is its ancestor. |
| Pattern merges | A merge writes `patterns_merged` and, when the same call changes fields, also `pattern_saved`, so each event means one thing. The merge happens before a rename, so the survivor can take the merged pattern's name. |
| Commands | `task-orchard mcp` names the stdio MCP server from Phase 2A. The bare command and `serve` keep running it until Phase 6. Then `serve` becomes the HTTP service and the bare command prints the help and exits non-zero (confirmed with the user), so a stale client configuration fails loudly instead of starting a web server. |
| Instance deadlines | A spawned instance's deadline has source `user`: it comes from the rule the user set, and triage must not guess it away. |
| First instance | `create_task` does not spawn a scheduled task's first instance; the next request does. `roll_forward` runs once, at the start of a request, so every spawn comes from the same place and `create_task` writes only `created`. |
| Converting a blocker | A plain task that blocks open tasks cannot become recurring, rather than having its links removed or moved to an instance. 7.3 forbids a recurring blocker; refusing keeps the call free of side effects, and the hint says to unlink first. |
| Recurrence in `updated` | Recorded as `changes.recurrence: {from, to}`, with the whole set of recurrence keys on each side, rather than one change per stored column (`rrule`, `recur_dtstart`, `lead_minutes`, ...). The wire keys are what the agent set and what it reads back. |
| Missed, in order | When the cap closes an instance, its dropped descendants are logged first, then `missed`, then the new instance's `spawned`: each event follows the change it reports. |
| UNTIL as a date | Means the end of that day, not its start, so a rule "until 20 March" with a 09:00 due time still has 20 March's slot. |
| Reopening a flexible instance | Does not undo the snooze its completion set on R; the response carries a `note` saying R is asleep and how to wake it. Undoing it would need the snooze from before the completion, which nothing stores. |
| Waking a scheduled task that is not paused | `snooze_task(days=0)` on a scheduled task with no snooze writes nothing, as for any task. |
| Scenario hint | Rejecting `deadline`, `firmness` or `achievable` on a recurring task now says to call again without them and where firmness goes. Haiku sent all three for "pay the rent on the 1st" and took three calls to recover from the earlier hint. |
| Settings table | One row (`id = 1`, enforced by a CHECK), holding only the timezone for now. A stored zone also silences the preamble's timezone warning, since onboarding has confirmed it. |
| `set_timezone` result | `now` is shown in the zone just saved, so the model can read the change back to the user. When `TASK_ORCHARD_TZ` overrides the stored zone, the call still saves it and adds a `note`, rather than failing, so the setting is right once the environment variable goes. |
| Balance keys | `last_7_days` and `last_30_days`: the window in the key, so the model never has to be told what `week` means. |
| Digest numbers | One decimal place, and whole numbers without one. Fewer characters, and `45` reads as minutes where `45.0` reads as a measurement. |
| Duplicates as pairs | Every pair, ordered by id, so a title held by three tasks is three pairs and the agent can merge them one at a time. |
| Missed counts | `missed.done` counts completions in the same 30 days as `missed`, so the two can be compared. |
| Untouched | Recurring tasks are left out: they are containers and are never touched themselves, so they would fill the section. Their instances count as ordinary tasks. |
| Closed in activity | `dropped` and `missed` are read from status plus the closing event in the window, so a reopened task is not reported as dropped. A merge is reported as dropped with `reason: merged into #N`, since it is how the user lost a task from the list. |
| `get_recent_activity` default | One day (the last 24 hours), as 8.16 states, for the evening run of Phase 6. The guides that want a week pass `days: 7`. The first build defaulted to 7 against the spec and was put back. |
| Merge notes | Cut to fit the 20,000-character notes limit with a marker pointing at the source, rather than refusing the merge: a merge is for duplicates, and the source stays readable. |
| Merge events | The source is logged as `merged` only, not also `dropped`, so each event means one thing and the activity report shows "merged into" in its place. |
| Input limits | Found by `test_response_size.py`: conditions at most 5 tags of 30 characters, a pattern's `description` at most 600 characters, and the pattern name in a row cut to 40. Without them a single row could pass 2,000 characters. Accepted by the user for the shakedown; revisit if real use runs into them. |
| Worst-case rows | With every key at its limit (an 80-character title, five long conditions, a 40-character pattern name, a 120-character hint, three blockers) a row is about 870 characters, so a list of 30 such rows passes 20,000. Real rows are far shorter; realistic data stays under 20,000 for every read tool, and the worst case is held under 30,000 by a test. The user accepted this: only a deliberately pathological list comes near it, and a large tool result does not break the app (nothing caps or rejects one), so the test's job is only to notice growth. |
| Housekeeping and dates | Housekeeping clears a stale soft deadline the user did not set, and never picks a new date. The first draft of section 9 allowed re-dating; but housekeeping runs with nobody there to answer (Phase 6), and a date the user did not choose is a guess that looks like a commitment. |
| Onboarding trigger | `overview` offers onboarding when the profile has no charter. The trigger lives in the guide, not in code, since the app judges nothing in code. |
| Overview length | `overview` may be 800 words: it lists every guide and says when to use each, and the list grows with every phase. The others keep the 600-word limit, checked by a test. |
| Tiers after Phase 5 | Every scenario passes on Haiku 5.5. On the strategic scenarios Sonnet 5.5 was better at two things. It was more decisive when a block was too short for its deadlines: it committed to the plan that met both and said there was no buffer, while Haiku varied between runs and once also offered a plan that missed a deadline. It also made the first step of a breakdown smaller. Haiku invented a condition tag during a breakdown. Onboarding and the weekly review were much the same on both. The user chose to keep Haiku 5.5 as the strategic default: the stack is meant to run on the small model, and a larger one is one setting away. |
| Recurring duplicates at triage | Before converting recurring intent, triage searches for a recurring task that already covers it. A scenario turned "running twice a week" into a second flexible task beside "Go for a run": the titles differ, so the exact-title duplicate check could not catch it. |
| Sources for untracked fields | Haiku passed `sources: {"recurrence": "user"}` every time it set up a recurring task, and lost a call to the `invalid` that followed: the user did state the recurrence, so marking it was right in spirit. An entry naming a real field that carries no source is now ignored; only unknown names, which are typos, are refused. |
| `get_guide` in the guides | The overview told the model to call `get_guide` "if the guides are not already in front of you". In the app they are, but Haiku still looked for the tool, which only the MCP adapter has. No guide names it now (a test checks), and the preamble says the guides are all in the prompt. |
| Chat input and transcripts | From the user's testing: `/exit` is accepted as well as `/quit`; input goes through `readline`, since without it arrow keys were echoed as escape codes and the cursor drifted from the text (seen over ssh in a shpool session); and a transcript is kept by default, in the user's cache directory rather than the project. |

## 14. Future work

Questions deliberately left open and work deliberately put off, so they are not lost. Each says what would bring it back. Work planned for Phase 6 is in section 12, not here.

| Item | Where it stands |
|---|---|
| Suggestions offered and not chosen | Whether to log the options an agent showed the user and the one they took, as a signal of its own: closer to "avoided" than "engaged", and deliberately not a touch (section 5.6). The server cannot see what an agent showed the user. The app can: its loop holds the shortlist the model was given and the answer it wrote, so it could record the offer itself at no cost in tool calls. An outside assistant would have to report it. The user is watching for a need; revisit if `deferred` and `starts` turn out not to show what keeps being avoided. |
| Compaction | The API's server-side compaction for long app sessions, in place of ending them with `/new` (section 10.2). It fits the append-only rule. Comes with the wider work on how the app manages context. |
| The model choosing its tier | Letting the model ask for the strategic tier itself, rather than the user choosing (section 10.2). |
| Choosing guides per session | The app puts every guide in the prompt. As guides grow, it may pay to load only the ones a session needs. |
| Semantic duplicates | Only equal normalised titles are flagged as duplicates (section 6.5). Near-duplicates ("running twice a week" and "Go for a run") are left to the agent's judgement in triage and the weekly review. Revisit if they pile up. |
| Delivering reports | Showing an unattended run's report without the user opening a session (Phase 7, section 12). |
