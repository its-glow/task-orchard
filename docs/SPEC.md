# Task Orchard — Feature Specification

This is the build document. It says exactly what each part does, so that implementation needs no further design decisions. `IMPLEMENTATION_PLAN.md` explains why the design is what it is; read it once for context. Where the two differ, this document wins (section 13 lists the refinements).

**Status.** Phase 0 (scaffold and schema) is done and tested. Phases 1–6 are specified here and not yet built.

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
10. The MCP server
11. Phases and acceptance
12. Phase 6: HTTP and agents
13. Refinements over the plan

---

## 1. Ground rules

1. **The server computes facts; the agent makes judgements. This is a default, not a prohibition.** The server owns invariants, arithmetic, the clock and retrieval, and never calls an LLM. Retrieval already involves simple heuristics: choosing which 20 of 200 tasks the model sees is one (section 5.7). A server-side heuristic is acceptable when all of these hold:
   - it decides what to **show**, not what to do;
   - it can be stated in a sentence;
   - it is visible: the response says what was left out and why;
   - the caller can adjust or switch it off with a parameter.

   What stays out of the server is opaque scoring: weighted formulas whose result the agent can neither explain nor override. When unsure, start with the agent deciding, and move logic into the server once real use shows the agent is being handed too much or is deciding badly.
2. **Built for a small model.** Assume the caller is Haiku-class. Facts arrive ready to use. Tools validate everything and explain failures with a hint. A multi-step change is one atomic call.
3. **Derived, not stored.** Nothing in section 5 or 6.1 is ever written to a column.
4. **Context is a budget.** Every response is bounded (limits are stated per tool). Lists return compact rows. History reaches the model only as aggregates.
5. **Every mutation is logged** in the same transaction as the change (section 6.4).
6. **Layering.** `core/` imports nothing from `mcp/` or `api/`. A tool or route calls one `Orchard` method and returns its dict. All logic is in `core/`.

## 2. Glossary

| Term | Meaning |
|---|---|
| Task | A row in `tasks`. May have children. A **plain** task is one that is not recurring. |
| Recurring task | A task with `recur_mode` set, to `scheduled` or `flexible`. Never completed; only dropped (retired) or snoozed (paused). There is no separate type field: being recurring is having a mode. |
| Instance | A task whose parent is a recurring task. One occurrence of it. |
| Slot | One date and time produced by a scheduled task's rule. A scheduled instance stands for exactly one slot (`occurrence_at`). |
| Open | `status = open`. Closed means `done`, `dropped` or `missed`. |
| Leaf | An open task with no open children. |
| Own value | What is stored on the task itself. |
| Effective value | What the task has once inheritance through parents and blockers is applied (section 5.4). |
| Dormant | Hidden by a snooze on the task or on any ancestor. |
| Blocked | Waiting on an open blocker of the task or of any ancestor. |
| Candidate | A task that could be worked on now (section 5.3). |
| Inbox | Tasks with `triaged = false`. |
| Pattern | A named kind of work, with statistics and a short learned note. |
| Charter | The profile note with topic `charter`: the user's statement of what Task Orchard is for. |
| Row | The compact JSON form of a task used in lists (section 4.3). |
| Detail | The full JSON form of one task (section 4.4). |

## 3. Time

### 3.1 Storage and the clock

- Every instant is stored in UTC and handled as an aware `datetime`. The one exception is `Task.recur_dtstart` (section 7.1).
- `tx.now` is the only source of the current time. Nothing calls `datetime.now()`.
- The user's timezone is `tx.tz`. All parsing and formatting below use it.

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

"Today" runs from local midnight to local midnight. "Within N days" in this document means within N × 24 hours of `now` unless it says "calendar days".

**A snooze counted in days ends at the start of a day.** When a snooze is set by a number of days (`snooze_task(days=)`, or a flexible task's `gap_days`), it ends at local midnight at the start of the day that falls that many × 24 hours after the starting instant. A run finished on Monday evening with a gap of 2 is back on Wednesday morning, not Wednesday evening.

## 4. Wire conventions

### 4.1 General

- Every tool returns one JSON object. Every object includes `now`: a stamp without `rel`. (`get_guide` is the one exception.)
- **Enumerated values are exchanged by name**: `"high"`, `"have_to"`, `"soft"`. Numbers are never used for scales. An unknown name raises `invalid` and lists the valid names.
- **Empty keys are omitted.** A key whose value would be null, `[]`, `{}`, `false` or `0` is left out, unless this document says "always present".
- Titles in rows are truncated to 80 characters with a trailing `…`.
- Ids are integers. A parameter that takes several ids is a JSON array.

### 4.2 Errors

The service raises `OrchardError(code, message, hint, details)`. The MCP layer returns it as a tool error whose text is this JSON:

```json
{"error": "cycle", "message": "That would create a loop ...", "hint": "Remove one of ...", "details": {"path": [...]}}
```

Codes are fixed in `core/enums.py::ErrorCode`. Every error has a `hint` that tells the model what to do next. A failed call changes nothing.

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
| `path` | Ancestor titles from the root down to the parent, each truncated to 40 characters. Omitted for roots. |
| `priority` | Effective priority. Omitted when neither the task nor anything it inherits from sets one (section 5.4). |
| `priority_via` | `{id, title, how}` when the effective priority is not the task's own. `how` is `"parent"` or `"blocks"`. |
| `priority_own` | The task's own priority, only when it differs from the effective one. |
| `due` | The earliest effective deadline: a stamp plus `firmness`, plus `via` (as above) when inherited. |
| `hard_due` | Same shape. Present only when `due` is soft and a later hard deadline also applies. |
| `estimate_minutes` | |
| `activation`, `drain` | |
| `motive` | Effective motive. |
| `achievable` | `"no"` or `"unknown"`. Omitted when `yes`. |
| `state` | Array of flags that apply: `inbox`, `dormant`, `blocked`, `has_open_children`, `overdue`, `at_risk`. |
| `snoozed_until` | Stamp, when dormant by the task's own snooze. |
| `blocked_by` | Up to 3 open effective blockers as `{id, title}`, plus `blocked_by_more: n` if there are others. |
| `unblocks` | Count of open tasks this task explicitly blocks. |
| `conditions` | |
| `touched` | `rel` of `last_touched` (section 5.6), when more than 1 day ago. |
| `deferred` | Times deferred (section 5.6). |
| `starts` | Times started (section 5.6). |
| `recurring` | For recurring tasks: `{mode, cadence, gap_days, last_done, done_count, past_wake_days, next_slot}` as applicable (section 7). |
| `pattern` | `{id, name, hint, ratio}`. `hint` is the pattern's `learned` note cut to 120 characters. `ratio` is the estimate ratio (section 6.1) when it rests on at least 3 completions. |

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
- **Protection.** If a field's current source is `user` and the incoming source for it is not `user`, that field is left unchanged. The rest of the call still applies. The response lists each such field under `skipped`: `{field, kept, proposed, reason: "set by the user"}`. If the incoming source was `pattern` or `evidence`, a `conflict` event is also written (section 6.4).
- Clearing a field removes its source entry.
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
3. If one exists, raise `cycle`. `details.path` is the loop as a list of `{id, title, because}`, where `because` is `"child of"`, `"blocks"` or `"blocks an ancestor of"` and describes the edge to the next entry.

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

All three are computed by memoised recursion along the edges of 5.1, which is safe because the graph is acyclic. Only open tasks take part; a closed task contributes nothing.

**Effective priority** of T is the highest of:

- T's own priority, if set;
- the effective priority of T's parent;
- the effective priority of every open task T effectively blocks.

If none of these exists, the task has no effective priority: the key is omitted from rows, and wherever tasks are ordered or filtered by priority it counts as `normal`. The source is reported as `via`: the parent, or the blocked task that supplied the value (the explicitly blocked one, even when the value came up through its descendant). Ties prefer own, then parent.

**Effective deadline** is computed twice, once over all deadlines and once over hard deadlines only, giving `due` and `hard_due`. Each is the earliest of:

- T's own deadline (for the hard pass, only if its firmness is hard);
- the effective deadline of T's parent;
- for every open task B that T effectively blocks: B's effective deadline minus `remaining_minutes(B)`.

Firmness and source travel with the winning value. `hard_due` is reported only when it is later than `due`.

**Effective motive** is T's own motive, or else that of the nearest ancestor that has one. It does not travel through blocker links.

Required property (test it): removing a link or moving a task changes effective values on the next read with no other action.

### 5.5 Remaining work, overdue and at risk

- `remaining_minutes(T)`: if T has open children, the sum of `remaining_minutes` over them; otherwise T's `estimate_minutes`, or 0 if unset.
- A deadline is **overdue** when it is in the past.
- A deadline is **at risk** when it is not overdue and falls within the larger of 24 hours and `3 × W` from now. W is the work that has to fit before that deadline: `remaining_minutes` of the **highest ancestor of T, or T itself, that has the same effective deadline** in that pass.
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
| `last_touched` | The latest event on the task whose type is not in `SYSTEM_EVENTS`; if none, `created_at`. |

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
6. **Reserve wants.** If fewer than `reserve_wants` rows have effective motive `want`, bring in the highest-ordered `want` candidates that were cut, each replacing the lowest-ordered row that is in group `other` and is not a `want`. Stop when the reserve is met, when no cut `want` remains, or when no replaceable row remains. Re-sort by the order of step 4. Rows brought in this way carry `reserved: true`.

Each row carries, besides section 4.3: `group`, and `over_energy: true` when the caller gave `energy` and the task's `drain` is higher. Energy never filters.

The response also reports what was left out, so the agent knows the shortlist is partial (section 8.13).

### 5.8 The critical list (`get_critical`)

All **leaves** (open, no open children, not recurring, triaged), whether or not they are blocked or dormant, for which any of these holds:

- overdue;
- at risk;
- `due` within `horizon_days`;
- effective priority is `critical`.

Ordered by `due` (none last), then effective priority, then id. At most 30 rows, with `omitted` if more. Rows include `blocked_by` in full, since explaining what stands in the way is the point of this tool.

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
| `ratio` | Median of `actual_minutes / estimate_at_completion` over the 20 most recent completions that have both, to one decimal place. `ratio_samples` gives the count. |
| `felt_activation`, `felt_drain` | Median over the 20 most recent completions that have the value, rounded to the nearest level (ties round up), as a name. |
| `last_done` | Stamp of the most recent completion. |
| `gaps_days` | Days between consecutive completions, most recent 5 gaps, one decimal place. Only when the pattern has at least 2 completions. |

A digest is at most this fixed set of keys. It is the only form in which history reaches the model.

### 6.2 Profile and charter

- One note per topic; topics compare without regard to case.
- Topic `charter` is the charter. Limit 4,000 characters.
- Every other note: limit 1,000 characters each, 8,000 in total.
- A write that would exceed a limit raises `profile_full`, with the current sizes in `details` and the hint to consolidate existing notes first.

### 6.3 The balance

For each of the windows "last 7 days" and "last 30 days" (by `completed_at`):

```json
{"minutes": {"want": 240, "should": 90, "have_to": 1260, "unset": 45},
 "completions": {"want": 2, "should": 3, "have_to": 19, "unset": 1},
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
| `create_task` | `created` (new task) `{via: "create", parent_id}`; `linked` per link; `updated` (parent) when its `achievable` was flipped (section 8.2); `pattern_saved` when a pattern was created for a new recurring task |
| `update_task` | `updated` (task) `{changes: {field: {from, to}}}`; plus `paused` or `resumed` (task) when a scheduled task's own snooze is set or cleared; plus `conflict` (task) `{field, kept, proposed, source}` per protected field skipped with source `pattern` or `evidence`; plus `updated` (new parent) when a move flipped its `achievable`; plus `pattern_saved` when converting to recurring created a pattern |
| `subdivide_task` | `created` (each child) `{via: "subdivide", parent_id}`; `linked` per ordering link; `subdivided` (parent) `{children: [ids]}` |
| `link_tasks` | `linked` (blocked task) `{blocker_id, blocked_id}` |
| `unlink_tasks` | `unlinked` (blocked task) `{blocker_id, blocked_id}` |
| `start_task` | `started` (task) `{}` |
| `complete_task` | `completed` (the task that became done) `{actual_minutes, completed_at, recurring_id?, sleeping_until?}`; preceded by `created` `{via: "complete"}` when the task was created by this call, or by `spawned` when a scheduled task's next instance was created by this call; preceded by `dropped` `{reason: "parent completed", cascade_from}` for each open child dropped; plus `pattern_saved` when `new_pattern` created one |
| `snooze_task` | `snoozed` (task) `{until, reason, previous}`; or `woken` (task) `{previous}`; for a scheduled recurring task `paused` `{until, reason}` or `resumed` `{skipped_until}` instead |
| `drop_tasks` | `dropped` (each task, including descendants) `{reason, cascade_from?}` |
| `reopen_task` | `reopened` (task) `{was, completion?}` where `completion` holds the deleted completion row's fields |
| `merge_tasks` | `merged` (source) `{into, moved_children, moved_links, filled}`; `updated` (target) `{changes}` if any of its fields changed |
| `save_pattern` | `pattern_saved` (no task) `{pattern_id, created?, changes}`; or `patterns_merged` (no task) `{from: {id, name}, into, tasks_moved}` |
| `write_profile_note` | `profile_saved` (no task) `{topic, deleted?}` (the text is not copied into the log) |
| `roll_forward` | `spawned` (instance) `{recurring_id, occurrence_at}`; `missed` (instance) `{recurring_id, replaced_by}`; `resumed` (recurring) `{skipped_until}` |

`{from, to}` values use the wire form (names, stamps' `at` strings).

### 6.5 The review queue (`get_review_queue`)

Each section is a list of rows with at most 10 entries, plus a total count. The server applies no thresholds beyond "greater than zero"; it sorts and cuts, and the agent decides what is worth raising.

| Section | Contents, in order |
|---|---|
| `inbox` | Untriaged open tasks, oldest first. Limit 30. |
| `parent_checks` | Open plain tasks that have at least one child and no open children. |
| `overdue` | Open leaves whose own deadline has passed, longest overdue first. Each row adds `overdue_days`. |
| `most_deferred` | Open tasks with `deferred > 0`, highest first. |
| `repeat_starts` | Open tasks with `starts > 1`, highest first. |
| `untouched` | Open triaged leaves, by `last_touched`, oldest first. Each row adds `untouched_days`. Dormant tasks are excluded. |
| `duplicates` | Pairs of open tasks whose normalised titles are equal: `{a: {id, title}, b: {id, title}}`. |
| `conflicts` | `conflict` events from the last 30 days on tasks that are still open: `{task: {id, title}, field, kept, proposed, source, when}`. |
| `missed` | Per scheduled recurring task with instances missed in the last 30 days: `{recurring: {id, title}, missed: n, done: n}`. |

A **normalised title** is lowercased, with every character that is not a letter or digit replaced by a space, and runs of spaces collapsed.

### 6.6 Recent activity (`get_recent_activity`)

For the last `days` (1 to 31):

- `completed`: up to 30 `{id, title, actual_minutes, motive, when}`, newest first, plus `completed_count`.
- `dropped`, `missed`: up to 10 each as `{id, title, when, reason?}`, plus counts.
- `created_count`, `deferred_count`, `started_count`.
- `balance`: section 6.3, always both windows whatever `days` is.

## 7. Recurrence

Implement in `core/recurrence.py`. Only this module and `complete_task` create instances.

### 7.1 Scheduled tasks

Stored fields: `rrule`, `recur_dtstart`, `lead_minutes`, `recur_firmness`, `last_spawned_slot`, and optionally `cadence` as a human description.

**The rule.**

- `rrule` is an RFC 5545 RRULE value such as `FREQ=WEEKLY;BYDAY=FR`. A leading `RRULE:` is stripped. Text containing `DTSTART` or a newline is rejected.
- `FREQ` must be `DAILY`, `WEEKLY`, `MONTHLY` or `YEARLY`. Anything more frequent is rejected with `invalid`.
- The rule must parse with `dateutil.rrule.rrulestr` and must produce at least one slot after `now`.
- It is evaluated on **naive local wall-clock time**, starting from `recur_dtstart`. Each slot is then localised to the user's timezone (section 3.2 rules for missing and ambiguous times) and converted to UTC. This keeps "Friday 17:00" at 17:00 local across daylight-saving changes.
- `recur_dtstart` is set when the rule is created or changed: today's local date at the rule's due time. The due time comes from the `due_time` parameter (`HH:MM`, default `23:59`).
- A rule with no `BYDAY`, `BYMONTHDAY` or similar therefore repeats from the day it was created. The create response shows the next three slots so the agent can confirm the rule means what the user meant.

`next_slot(R, after)` is the first slot strictly later than `after`. `after` is converted to local wall-clock time before the rule is consulted.

**Creation.** `last_spawned_slot = now`, so the first instance is for the first slot after creation.

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
        close live[0]: status = missed, closed_at = now
        record MISSED on it {recurring_id, replaced_by: <new instance id>}
    create the instance for `slot`
    record SPAWNED on it {recurring_id, occurrence_at}
    R.last_spawned_slot = slot
    slot = next_slot(R, after=slot)
    spawned += 1
```

- An **instance** is created with: R's `title`, `description`, own `priority`, `estimate_minutes`, `activation`, `drain`, `conditions`, `achievable`, `pattern_id` and the matching `field_sources` entries; `parent_id = R.id`; `occurrence_at = slot`; `deadline = slot`; `deadline_firmness = R.recur_firmness`; `motive` unset (it inherits); `snooze_until` unset; `triaged = true`.
- **Idempotent:** calling it twice with the same `now` changes nothing the second time. The unique index on `(parent_id, occurrence_at)` backs this up.
- **Lockstep:** spawning depends only on the clock and `last_spawned_slot`, never on when an instance was completed.
- **Cap:** at most two open instances per scheduled task. After a long absence the loop walks every due slot, so the skipped ones exist in history as `missed`.
- The limit of 60 per request only bounds one request; the next request continues.
- Only R's **own** snooze pauses it. A snooze on an ancestor hides R's instances but does not stop them spawning.

**Pausing.** Setting R's own `snooze_until` (through `snooze_task` or `update_task`) is a pause: `paused` is logged, open instances become dormant, and nothing spawns. When the pause ends, by the clock or by waking, `last_spawned_slot` jumps forward to the moment the pause ended (never backward), so the paused slots are skipped, and `resumed` is logged.

**Completing R directly.** `complete_task` on a scheduled recurring task means "this occurrence is done":

- if R has open instances, the oldest is completed;
- otherwise the next slot's instance is created now (`spawned` is logged), `last_spawned_slot` moves to that slot, and that instance is completed. This is the "did Friday's email on Tuesday" case.

**Changing the rule** (`update_task` with `recurrence`): `recur_dtstart` is recomputed from today, `last_spawned_slot` becomes `max(last_spawned_slot, now)`, and existing instances are left alone.

Manual children cannot be added to a scheduled task (`unsupported`).

### 7.2 Flexible tasks

Stored fields: `cadence` (free text for the agent) and optionally `gap_days`.

**Completion.** When an instance of flexible R is completed, or R is completed directly:

1. If R was completed directly and has an open instance, that instance is the one completed. If it has none, a done instance is created under it to hold the completion (title and fields copied as in 7.1, no `occurrence_at`, no deadline). R itself stays open.
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
- `create_task(parent_id=R, ...)`: spawn a concrete instance. Any field not given is copied from R. At most one open instance may exist (`instance_exists`).

**In lists.** The `recurring` block of a flexible task's row has `mode`, `cadence`, `gap_days`, `last_done` (rel), `done_count`, and `past_wake_days`: whole days since `snooze_until`, when `gap_days` is set and the task is awake.

`snooze_task` on a flexible task, or on its instance, **is** a deferral and is counted.

### 7.3 Shared rules

- A recurring task cannot be a blocker (`unsupported`: block with its instance instead). It can be blocked; its instances inherit that.
- A recurring task's parent cannot be recurring. An instance cannot be recurring.
- A recurring task may have an ordinary parent. While it is open it counts as an open child, so that parent is not a leaf.
- Dropping a recurring task retires it and drops its open instances.
- When a recurring task is created without a `pattern_id`, the pattern whose name equals its title (ignoring case) is used, or created. Its instances inherit the pattern.
- A plain task can be converted to recurring through `update_task(recurrence=...)` if it is open, has no children, is not an instance, and has no deadline (or the same call clears it). The reverse is `unsupported`.

## 8. Operations

Each operation is one `Orchard` method and one MCP tool of the same name. "Phase" says when it is built. Parameters marked `?` are optional. Unless stated, a task id that does not exist raises `not_found`.

Every write returns the affected task as a detail (section 4.4) under `task`, unless stated otherwise.

### 8.1 `capture_tasks` — Phase 1

Put things in the inbox with no questions asked.

- `items`: 1 to 20 of `{title, notes?}`.
- Creates each as an open, untriaged, root task.
- Returns `{captured: [{id, title}], possible_duplicates: [{new_id, existing: {id, title}}]}`. A possible duplicate is an existing open task with the same normalised title.

### 8.2 `create_task` — Phase 1 (recurrence in Phase 4)

Create a triaged task with whatever is known.

| Parameter | Notes |
|---|---|
| `title` | Required, non-blank, at most 500 characters. Optional when the parent is a flexible recurring task, in which case it is copied from the parent. |
| `description?`, `notes?` | |
| `parent_id?` | Must be open (`parent_closed`). |
| `priority?` | `low`, `normal`, `high`, `critical`. |
| `deadline?`, `firmness?` | `deadline` per section 3.2. `firmness` defaults to `soft`. `firmness` without `deadline` is `invalid`. |
| `snooze_until?` | Section 3.2. |
| `achievable?` | `yes`, `no`, `unknown` (default). |
| `estimate_minutes?` | Integer above 0. |
| `activation?`, `drain?` | `low`, `medium`, `high`. |
| `motive?` | `want`, `should`, `have_to`. |
| `conditions?` | Array of tags. Each is lowercased and trimmed, with spaces turned into hyphens. |
| `pattern_id?` | |
| `blocked_by?`, `blocks?` | Arrays of ids; each must be open. Cycle-checked together with the parent. |
| `recurrence?` | Section 8.2.1. |
| `sources?` | Section 4.5. |

Rules:

- Adding a child to a task (here, through `subdivide_task`, or by a move) sets that parent's `achievable` to `no` if it was not already. This is structural, so it applies even to a `user`-sourced value; the source becomes `evidence`.
- If the parent is a flexible recurring task, this spawns an instance (section 7.2). If it is scheduled, `unsupported`.
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
| `gap_days` | flexible | None. Above 0. |

A key given for the wrong mode is `invalid`.

### 8.3 `update_task` — Phase 1 (recurrence in Phase 4)

Change any stored field of a task.

- `task_id`, then any of the `create_task` field parameters except `blocked_by` and `blocks`.
- `append_note?`: text added to the end of `notes` on a new line.
- `clear?`: array of field names to unset. Allowed: `description`, `notes`, `priority`, `deadline`, `snooze_until`, `estimate_minutes`, `activation`, `drain`, `motive`, `conditions`, `pattern`, `parent`, `cadence`, `gap_days`. A name that is also being set in the same call is `invalid`.
- `triaged?`: default `true`. So any update takes a task out of the inbox unless `triaged: false` is passed.
- `recurrence?`: on a plain task, converts it (section 7.3). On a recurring task, a partial object changes only the keys given; `mode` cannot change.

Rules:

- Omitted means unchanged. Unsetting is done only through `clear`.
- Setting `parent_id` moves the task. The new parent must be open; the move is cycle-checked. Instances cannot be moved, and nothing can be moved under a recurring task this way.
- On a **closed** task only `description`, `notes`, `append_note` and `pattern_id` may change, and `triaged` is left as it is. Anything else is `not_open`.
- Protected fields are skipped and reported (section 4.5).
- Returns `{task, skipped?}`. A call that changes nothing returns the task and writes no event.

### 8.4 `subdivide_task` — Phase 3

Break a task into children in one atomic call.

- `parent_id`: an open plain task. A recurring task is `unsupported` (subdivide its instance).
- `children`: 1 to 12 of `{title, description?, estimate_minutes?, achievable?, priority?, activation?, drain?, conditions?, after?}`.
- `after?`: positions (counting from 1) of other children in this same call that must be finished first.
- `sequential?`: default `false`. When `true`, each child is blocked by the one before it, and `after` must not be used.
- `sources?`: applies to every child.

Effects: children are created triaged; the parent's `achievable` becomes `no`; ordering links are created and cycle-checked.

Returns `{parent: row, children: [rows]}`.

### 8.5 `link_tasks`, `unlink_tasks` — Phase 1

- `blocker_id`, `blocked_id`.
- `link_tasks`: both must be open; they must differ; the blocker must not be recurring; cycle-checked. Linking an existing link is not an error: returns `{already_linked: true}`.
- `unlink_tasks`: removing a link that does not exist returns `{was_linked: false}`.
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
| `parent_id?`, `motive?`, `description?` | Only with `title`. A recurring `parent_id` is `invalid`: pass the recurring task as `task_id` instead. |
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
- Writes the `completions` row with `estimate_at_completion` and `motive_at_completion` (the **effective** motive at this moment).
- Sets `status = done` and `closed_at = now`. When the work was actually done is `completed_at` on the completion row, which may be earlier.

Returns `{completed: row}` plus, when they apply:

- `parent_check`: when the parent is a plain task, is open and now has no open children: `{parent: row, children: [{title, status}]}` (up to 10, most recently closed first).
- `recurrence_check`: section 7.2.
- `next_slot`: a stamp, for a scheduled instance.
- `unblocked`: rows of tasks for which this was the last open effective blocker.

### 8.8 `snooze_task` — Phase 3

The user is putting something off, or a recurring task is being paused.

- `task_id`: must be open.
- `until?` (section 3.2) or `days?` (number, rounded as in section 3.4). Exactly one.
- `reason?`.
- If the resulting time is not after `now` (including `days: 0`), the snooze is cleared instead: this is how to wake a task.
- Events per section 6.4. Returns `{task}`.

### 8.9 `drop_tasks` — Phase 3

Close tasks that will not be done.

- `task_ids`: 1 to 50. `reason`: required, non-blank.
- Each open task is set to `dropped` with `closed_at = now`, and so is every open descendant.
- An id that is already closed is not an error; it is listed in `skipped`.
- Returns `{dropped: [{id, title}], also_dropped: [{id, title}], skipped?, parent_checks?}`.

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
- For each tracked field the target lacks and the source has, the value and its source entry are copied.
- The source's title, description and notes are appended to the target's notes under a line `Merged from #<id>: <title>`.
- The source becomes `dropped`.
- Returns `{task}` (the target).

### 8.12 `get_task`, `get_tree`, `search_tasks` — Phase 1

**`get_task(task_id)`** returns `{task}` as a detail.

**`get_tree(root_id?, depth?, include_closed?)`**

- `root_id` omitted: the forest of root tasks. `depth` 1 to 5, default 2. `include_closed` default `false`.
- Returns nested nodes `{id, title, status?, recurring?, state?, children?, more?}`, where `recurring` is the mode of a recurring task and `more` counts descendants below the depth limit.
- At most 100 nodes; if cut, `truncated: true`. Order within a level: open before closed, then id.

**`search_tasks(query?, status?, inbox_only?, limit?)`**

- `status`: `open` (default), `closed` or `any`. `limit` 1 to 50, default 20.
- `query` is matched with full-text search over title, description and notes. Split it into words, quote each one, and require all of them; if that finds nothing, retry requiring any of them. Never pass raw user text to `MATCH`.
- With no `query`, lists by `updated_at`, newest first.
- Returns `{results: [rows], total}`. Rows always include `status`.

### 8.13 `get_candidates` — Phase 2

The shortlist of section 5.7.

| Parameter | Notes |
|---|---|
| `limit?` | 1 to 50, default 20. |
| `under_id?` | Narrow to one subtree: "what's next on the release?" The id must exist (`not_found`). |
| `min_priority?` | `low`, `normal`, `high`, `critical`. Narrow to effective priority at or above this. |
| `due_within_days?` | Number from 0 to 365, fractions allowed. Narrow to what is overdue or due in that window. |
| `minutes_available?` | Integer above 0. |
| `exclude_conditions?` | Array of tags. |
| `energy?` | `low`, `medium`, `high`. Marks rows; never filters. |
| `reserve_wants?` | 0 to 10, default 3. |

The three narrowing filters exist so the agent can ask a sharper question when the default cut would be the wrong 20. For "is anything urgent", `get_critical` is the better tool, because it also shows urgent work that is blocked.

```json
{"now": {...},
 "candidates": [ <rows with group, reserved?, over_energy?> ],
 "pool": {"total": 34, "shown": 20, "cut": {"due_soon": 0, "other": 14}},
 "filtered": {"too_long": 3, "conditions": 1},
 "conditions_in_pool": ["at-home", "business-hours", "good-weather"],
 "inbox": 3}
```

`pool`, `inbox` and `conditions_in_pool` are always present, and the counts inside `pool` and `filtered` keep their zeros. `pool.total` counts candidates after filtering. `filtered` has one key per filter the caller applied (`outside`, `below_priority`, `not_due`, `too_long`, `conditions`) and none for the others. `conditions_in_pool` lists every tag carried by a candidate before filtering, so the agent knows which conditions are worth asking the user about.

### 8.14 `get_critical` — Phase 2

Section 5.8. `horizon_days?`: 0 to 30, default 1. Returns `{critical: [rows], omitted?, inbox}`.

### 8.15 `get_review_queue` — Phase 5

Section 6.5. No parameters. Returns one key per section, each `{items: [...], total}`.

### 8.16 `get_recent_activity` — Phase 5

Section 6.6. `days?`: 1 to 31, default 1.

### 8.17 `find_patterns`, `save_pattern` — Phase 3 (digest statistics in Phase 5)

**`find_patterns(query?, limit?)`**: full-text search over name, description and learned, with the same query handling as `search_tasks`. With no query: most recently used first (by latest member completion). `limit` 1 to 10, default 5. Returns `{patterns: [digests]}`.

**`save_pattern(pattern_id?, name?, description?, learned?, merge_from_id?)`**

- Without `pattern_id`: creates a pattern; `name` is required. If the name exists (ignoring case), `invalid` with the existing id in `details`.
- With `pattern_id`: changes the fields given.
- `learned` is at most 600 characters (`invalid` if longer, with the hint to rewrite it shorter, not to append).
- `merge_from_id`: moves that pattern's tasks to `pattern_id` and deletes it. The agent supplies the combined `learned` in the same call.
- Returns `{pattern: digest}`.

In Phase 3 a digest contains the stored fields and the status counts only; the remaining statistics arrive in Phase 5.

### 8.18 `read_profile`, `write_profile_note` — Phase 2

**`read_profile()`** returns `{charter?: {text, updated}, notes: [{topic, text, updated}], size: {used, limit}}`. Notes are ordered by topic.

**`write_profile_note(topic, text)`**: creates or replaces the note for that topic. Blank `text` deletes it. Limits per section 6.2. Returns the same shape as `read_profile`.

### 8.19 `get_guide` — done

Lists the guides, or returns one by name. Already implemented in `mcp/server.py`.

## 9. Skills

A skill is one markdown file in `src/task_orchard/skills/`, in the format `mcp/guides.py` parses: a header with `name` and `description`, then the instructions. It is served as an MCP prompt and through `get_guide`. `overview.md` exists.

Rules for writing every skill:

- Address the agent directly. State the goal first, then numbered steps.
- Name the exact tools to call. Assume a small model: be explicit about order and about when to stop.
- State what to do without asking and what to ask about.
- Keep each under about 600 words.
- When a phase adds tools, update `overview.md` if a concept changed, and update `INSTRUCTIONS` in `mcp/server.py` only if the session-start routine changes.

What each must contain:

| Skill | Phase | Required content |
|---|---|---|
| `triage` | 2 | Read the inbox (`search_tasks(inbox_only=true)`). For each task: guess every field from its wording, parent, `find_patterns` (from Phase 3) and the profile; write them with `update_task` and correct `sources`. The five rules of plan section 5.11, including the question budget: none at capture, at most one per task, batched across tasks. Spot recurring intent ("twice a week") and convert with `recurrence` (from Phase 4). Spot duplicates from `possible_duplicates`. End by telling the user, in two or three lines, what was assumed. |
| `what-now` | 2 | Map the request to a tool: one task or a few options → `get_candidates`; "what's urgent" → `get_critical`. Pass `minutes_available` and `energy` only if the user said them or they would change the answer. Narrow with `under_id`, `min_priority` or `due_within_days` when the user asks about one project, only important things, or a time window, or when `pool.cut` shows much was left out. Rank using the facts, the profile and the charter. Offer 1 to 5 options, each with a one-line reason drawn from the row's facts. When giving several, spread them across motives and say which is which. For a high-activation pick, propose a smaller first step. If a pick has `achievable` of `no` or `unknown`, hand off to `subdivide`. If the user declines an option as "not now", call `snooze_task`. When an offered task's soft deadline is long past, ask once whether to re-date it or clear it, so stale dates do not crowd the shortlist before housekeeping exists. |
| `subdivide` | 3 | Read the task with `get_task`. Break down **one layer only**, into 2 to 7 children that are each achievable or clearly need their own breakdown later. Use `sequential` or `after` only where order is real. Act alone when the breakdown is obvious; otherwise propose it and ask. Never plan the whole tree. Stop after two levels in one turn. Call `subdivide_task` once. |
| `complete-and-reflect` | 3 | Find the task first with `search_tasks(status="any")`; decide between existing, recurring and new (section 8.7). Ask one short question about how it went if the user did not say. Fill `actual_minutes`, `felt_activation`, `felt_drain` from their words. File a pattern: reuse when the match is clear, ask when not. Handle `parent_check`: close or continue without asking when obvious, and say what was done. Handle `recurrence_check` (Phase 4). Update the pattern's `learned` note when the reflection teaches something general. |
| `onboarding` | 5 | Explain the idea in four sentences. Draft the charter with the user and save it. Confirm the timezone shown in `now`. Take a brain-dump with `capture_tasks`, then run `triage`. Ask about fixed obligations and habits and set up recurrence. Seed two or three profile notes. |
| `plan-time-block` | 5 | Take the block length and energy. Call `get_candidates` with them and `get_recent_activity` for the balance. Read the charter. Build an ordered plan that fits, with a buffer of about a fifth of the block, a high-activation item early, and a share for wants as the charter directs. Present it as a short list with times. |
| `housekeeping` | 5 | Call `get_review_queue`. Without asking: merge exact duplicates, clear or re-date soft deadlines long past on low-priority tasks, consolidate profile notes that overlap. Never drop a task or change a `user`-sourced field without asking. Report what was done in a few lines and list what needs the user's decision. |
| `weekly-review` | 5 | With the user. Walk `get_review_queue` and `get_recent_activity(days=7)`: what was done, the balance against the charter, what keeps being deferred or restarted, missed recurrences, conflicts. For each stale item offer: drop, reshape, or recommit. Revisit the charter if the balance is far from it. |
| `morning-plan`, `evening-reflection` | 6 | For agents that initiate contact. Morning: `get_critical`, then a plan for the day. Evening: `get_recent_activity`, collect reflections on anything completed without one. |

## 10. The MCP server

- `mcp/server.py::build_server` registers every tool. A tool function does three things: accept arguments, call one `Orchard` method, return its dict. Wrap each with `guarded`.
- **Tool descriptions** are read by the model on every turn. Each says, in at most about 80 words: when to use the tool, in the user's terms; and anything surprising about it. Parameter descriptions give one example value each.
- Parameters use the wire forms of section 4: names for enumerated values, strings per section 3.2 for times. Annotate with `Literal[...]` so the schema lists the choices.
- Declare read tools with `annotations={"readOnlyHint": True}`. The bookkeeping `roll_forward` does at the start of a read does not count against this.
- `INSTRUCTIONS` tells the client to read the `overview` guide. From Phase 2, change it to: read the profile with `read_profile` at the start of a conversation, then the overview.
- Each skill is also registered as an MCP prompt (already wired).
- **Phase 1 check:** confirm in Goose that tools are callable, and note in `README.md` whether Goose surfaces MCP prompts and server instructions. `get_guide` works regardless.
- Transport is stdio through Phase 5. Remember that stdout belongs to the protocol.

## 11. Phases and acceptance

Work one phase at a time. A phase is done when its listed tests pass, `ruff check` is clean, and every earlier test still passes. Write the tests named here first.

Test at two levels: the service directly (`orchard.method(...)`), and each tool once through the in-memory MCP client to prove wiring, schema and error shape. Use the `orchard`, `clock` and `seeded` fixtures.

### Phase 0 — Scaffold and schema (done)

Project, schema, migrations, clock, config, events helper, transaction wrapper, demo seed, MCP shell with `get_guide`. Tests: `tests/test_schema.py`, `tests/test_scaffold.py`.

### Phase 1 — Tasks and graph

Build: `timefmt`, `views`, `graph`, the predicates of `derive`, and `capture_tasks`, `create_task` (without recurrence), `update_task` (without recurrence), `link_tasks`, `unlink_tasks`, `get_task`, `get_tree`, `search_tasks`.

Until Phase 2, rows show own priority and own deadline in place of effective ones.

Other row keys arrive with the phase that computes them: `deferred`, `starts`, `touched` and `pattern` in Phase 3; the `recurring` block in Phase 4; `pattern.ratio` in Phase 5.

The seed already contains recurring tasks, so the predicates must handle them from Phase 1. Until Phase 4, a write whose target is a recurring task raises `unsupported`; reads treat it like any other task, and its instances behave as plain tasks.

| Test module | Must prove |
|---|---|
| `test_timefmt.py` | Every form in 3.2, including what a bare day means for each kind of parameter, weekday-after-today, and both daylight-saving edge cases; day-counted snoozes ending at local midnight (3.4); every row of the `rel` table; rejection with the stated hint. |
| `test_graph.py` | Each rejection case in 5.2 with a readable path. Property test (hypothesis): after any sequence of accepted links and moves, the graph is acyclic. |
| `test_derive_predicates.py` | Each predicate, including dormancy and blocking inherited from an ancestor. Property test: no candidate is ever blocked, dormant, untriaged or a parent of open children. Against the seed: `offsite_venue` is blocked through `offsite`; `taxes` is dormant; `release_notes` is blocked; `cert` is a candidate. |
| `test_tasks_crud.py` | Create, update, clear, move; field sources and protection (4.5); closed-task restrictions; adding a child flips the parent's `achievable`; exact events per 6.4; a failed call writes nothing. |
| `test_read_tools.py` | Row and detail shapes, omission of empty keys, truncation, tree limits, search including quoting of hostile input such as `"`, `*` and `NEAR`. |
| `test_mcp_phase1.py` | Each tool callable through the client; an error arrives as JSON with `error` and `hint`. |

### Phase 2 — Prioritisation

Build: effective values and section 5.5 in `derive`, `shortlist`, `get_candidates`, `get_critical`, `read_profile`, `write_profile_note`. Skills: `triage`, `what-now`.

| Test module | Must prove |
|---|---|
| `test_effective.py` | Each rule in 5.4. Against the seed: `cert` has effective priority `critical` via `release_publish`, and a hard `due` 20 minutes (that task's estimate) before the release deadline; `game_build` has motive `want` via `game`. Unlinking `cert` restores `low` and no deadline. `hard_due` appears only when later than `due`. Property test: effective priority is never below own priority. |
| `test_shortlist.py` | Grouping and order per 5.7; each of the five filters alone and combined, its count, and first-filter-wins counting; `under_id` includes the task itself and rejects an unknown id; `over_energy` never filters; reserved wants are brought in, marked, and replace only non-want rows from `other`; `pool` counts add up. Against the seed: `release_flaky` and `cert` are `pressing`; `summary_w3` is present and overdue; `novel` or a `game_*` task is present even at `limit=5`. |
| `test_critical.py` | Blocked and dormant leaves are included with `blocked_by`; non-leaves are not; horizon boundaries. At risk is judged on the work of whoever owns the deadline (5.5): the leaves of a project due in three days with thirty hours of open work are listed, while a leaf with its own earlier deadline is judged on its own estimate. |
| `test_profile.py` | Upsert, delete by blank text, case-insensitive topics, each limit and `profile_full`. |

### Phase 3 — Leading edge and completion

Build: `subdivide_task`, `start_task`, `complete_task` (without recurrence), `snooze_task`, `drop_tasks`, `reopen_task`, per-task counts (5.6), `find_patterns`, `save_pattern` (stored fields and counts). Skills: `subdivide`, `complete-and-reflect`.

| Test module | Must prove |
|---|---|
| `test_subdivide.py` | Atomicity (one bad child creates none); `sequential`; `after` by position; cycle rejection; events. |
| `test_complete.py` | Completion row and snapshots (effective motive at the time); past `completed_at`; future rejected; untracked creation; `has_open_children` and `drop_open_children`; `parent_check` appears exactly when specified; `unblocked`. A parent left with no open children then appears in `get_candidates` with `achievable: "no"`. |
| `test_lifecycle.py` | Snooze, wake, `days: 0`; drop cascade and `skipped`; reopen rules and restoration of a deleted completion's data in the event; counts in rows (`deferred`, `starts`); `update_task(snooze_until=)` does not count as a deferral. |
| `test_patterns.py` | Create, rename, name clash, `learned` limit, merge moves tasks, search. |
| `test_events_exhaustive.py` | For every mutating operation: exactly the events of 6.4, in the same transaction. |

### Phase 4 — Recurrence

Build: `core/recurrence.py`, the `recurrence` object in `create_task` and `update_task`, recurrence paths in `complete_task`, `snooze_task`, `drop_tasks`. Change the seed so scheduled instances are produced by `roll_forward` where practical.

| Test module | Must prove |
|---|---|
| `test_rrule.py` | Validation rules; `next_slot` for weekly, monthly, month-end and yearly rules; 17:00 local stays 17:00 across the 29 March 2026 change in `Europe/London`. |
| `test_roll_forward.py` | First instance appears when the lead window opens; idempotence; lockstep whether the previous instance was done early, late or never; the two-instance cap and `missed`; a long absence leaves two open and the rest missed; pause and resume skip slots without marking them missed; the 60-per-request bound continues on the next request. |
| `test_recurring_complete.py` | Completing a scheduled task directly, with and without an open instance; flexible completion directly and through an instance; `gap_days` snooze; the recurrence check's contents; `instance_exists`; flexible task reappears as a candidate when awake with `past_wake_days`. |
| `test_recurring_rules.py` | Every rule in 7.3; conversion from a plain task; auto-pattern. |

### Phase 5 — Learning and review

Build: full pattern digests, the balance, `get_review_queue`, `get_recent_activity`, `merge_tasks`. Skills: `onboarding`, `plan-time-block`, `housekeeping`, `weekly-review`. Add skill scenarios.

| Test module | Must prove |
|---|---|
| `test_digest.py` | Every statistic in 6.1 on hand-built data, including medians on even counts, the 20-completion window, and exclusion of recurring containers. Against the seed: "Interface specs" has a `ratio` above 2. |
| `test_balance.py` | Windows, grouping, the `estimated` and `unknown_duration` rules. |
| `test_review_queue.py` | Each section's membership, order and limit. Against the seed: `dentist` leads `most_deferred` and is in `repeat_starts`; `milk` is in `overdue`; the weekly summary is in `missed`. |
| `test_merge.py` | Children, links, fields, notes; refusal cases; cycle check. |
| `test_response_size.py` | With 500 generated tasks, every read tool's response stays under 20,000 characters. |

**Skill scenarios.** Add `tests/scenarios/`: each scenario is a seeded database, a user message, and the expected tool calls and end state. They are run by hand against a chosen model, not in the normal test suite. Start with one per skill. Their purpose is to tell which skills a small model handles well enough.

Also revisit, with the user, whether to log suggestions that were offered and not chosen.

### Phase 6

Section 12.

## 12. Phase 6: HTTP and agents

To be detailed before it starts; the shape is fixed.

- **MCP over HTTP.** The same server, served with FastMCP's HTTP transport, selected by `task-orchard serve --http` with `TASK_ORCHARD_HOST` and `TASK_ORCHARD_PORT`. Bearer-token auth from `TASK_ORCHARD_TOKEN`; refuse to start on a non-loopback address without a token.
- **REST.** A FastAPI app in `src/task_orchard/api/`, mounted beside the MCP endpoint, same token. Read routes only at first, each calling the same `Orchard` method as its tool and returning the same JSON: `GET /candidates`, `/critical`, `/tasks/{id}`, `/tree`, `/search`, `/review`, `/activity`, `/patterns`, `/profile`. Writes are added when the Android app needs them.
- **One process, one writer.** SQLite is in WAL mode; all access goes through the single service process.
- **Agents.** Add `morning-plan` and `evening-reflection`. Write deployment notes for one agent platform (Hermes or OpenClaw, whichever the user picks), covering how skills are installed as `SKILL.md` folders and how the check-in schedule is configured there. Task Orchard itself never initiates contact.
- **Backups.** Document a nightly `sqlite3 .backup` of the database file.

The Android application (plan Phase 7) is outside this specification.

## 13. Refinements over the plan

Decisions made while specifying, which go beyond `IMPLEMENTATION_PLAN.md`:

| Topic | Refinement |
|---|---|
| Time input | A small fixed grammar (3.2) with weekday names and offsets, so the model does no date arithmetic. |
| Deadlines | Two effective deadlines, `due` and `hard_due`, so a soft early date does not hide a later hard one. |
| Shortlist | Three plain groups (`pressing`, `due_soon`, `other`) in place of a single "at risk first" rule; three named constants. Three optional narrowing filters (`under_id`, `min_priority`, `due_within_days`) under the agent's control. |
| Posture | "Facts in the server, judgement in the agent" is a default with a stated test for server-side heuristics (section 1), not a prohibition. |
| Deferral vs. scheduling | `snooze_task` counts as a deferral; `update_task(snooze_until=)` does not. This keeps the resistance signal clean when the agent sets a recurring task's return time. |
| Flexible instances | A flexible task has at most one open instance. |
| Early completion | Completing a scheduled task before its instance exists consumes the next slot. |
| Rule frequency | Scheduled rules are daily at most. |
| Capture | `capture_tasks` takes several items, for brain-dumps. |
| Tool names | `save_pattern` replaces `update_pattern`; `drop_tasks` is plural. |
| `update_task` | Unsetting is explicit, through `clear`, because JSON cannot distinguish "omitted" from "null" reliably across clients. |
| Completion row | Stores the effective motive at completion, so the balance survives re-parenting. |
| Closed status | `closed_at` records when the task was closed in the system; `completed_at` lives on the completion row, records when the work was done, and may be earlier. |
| Bare days | A date with no time means end of day for a deadline, start of day for a snooze, and midday for a completion. |
| Day-counted snoozes | End at local midnight, so a snoozed or resting task is back in the morning. |
| At risk | Judged against the remaining work of the whole project that owns the deadline, not the single task's estimate, so a large project is flagged before its last day. |
| Unset priority | A task with no priority of its own or inherited shows none, and is ordered as `normal`. |
| Conflicts | Evidence that disagrees with a user-stated value is logged as a `conflict` event and surfaced in the review queue. |
| Timezone | Read from the environment or the system, not stored in the database. |
| Event types | Not constrained in the database, so new types need no migration. |
| No `kind` field | The plan's `kind` (`task` or `recurring`) is gone. It duplicated `recur_mode`, which was added later; a task is recurring exactly when it has a mode. |
