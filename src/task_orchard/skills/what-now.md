---
name: what-now
description: Answer "what should I do now?" or "is anything urgent?" with one to five good options, each with a reason.
---
# What now

Goal: give the user a short, well-reasoned choice of what to do next, drawn from the facts the server gives you and the user's charter.

## Steps

1. Call `read_profile` if you have not this conversation. Follow the charter when ranking.
2. **Pick the tool.**
   - "What should I do?", "give me a few options" → `get_candidates`.
   - "Is anything urgent?", "what's on fire?" → `get_critical`. It also shows urgent work that is blocked, and what blocks it.
3. **Pass only what changes the answer.**
   - `minutes_available` and `energy` only if the user said them, or they would clearly change what you offer.
   - Narrow with `under_id` when the user asks about one project, `min_priority` when they want only important things, `due_within_days` for a time window.
   - If `pool.cut` shows many rows were left out and the shown ones do not fit the request, call again narrowed.
4. **Rank** the rows yourself, using:
   - `group` (`pressing` first unless the user says otherwise), priority, `due`, `state`;
   - `over_energy`: a draining task when the user is tired is a poor pick;
   - the charter and profile notes, for example time kept for things the user wants to do.
5. **Offer 1 to 5 options**, each with one line of reason taken from the row ("due tomorrow, and the release waits on it"). When you give several, spread them across motives and say which is which ("one for work, one you wanted to do").
6. **Shape each option.**
   - If a pick has a high `activation`, suggest a small first step ("just open the form").
   - If a pick has `achievable` of `no` or `unknown`, it needs breaking down first. If you have a `subdivide_task` tool, read the `subdivide` guide and offer to do that.
   - A row with `recurring` is a flexible recurring task. Offer it as itself ("go for a run?"). Completing it records one occurrence.
7. **The inbox.** If `inbox` is above zero and the user has time, offer to triage first (the `triage` guide), because inbox tasks are never offered as options.
8. **When the user turns an option down** as "not now", call `snooze_task` if you have it. Otherwise say it stays on the list.
9. **Stale dates.** If an offered task's soft deadline is long past, ask once whether to set a new date or clear it, and do that with `update_task`.

## Do not

- Calculate dates. Use the `rel` phrases in the results.
- List more than five options, or repeat the whole shortlist.
- Hide a `pressing` task without saying so.
