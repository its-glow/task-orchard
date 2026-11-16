---
name: complete-and-reflect
description: Record that something was done, with how it went, so later estimates and suggestions improve. Handle what finishing it changes.
---
# Complete and reflect

Goal: when the user says something is done, record it against the right task with how long it took and how it felt, file it under the right kind of work, and deal with what finishing it changes. One short question at most.

## Steps

1. **Find the task.** Call `search_tasks` with words from what the user said and `status: "any"`. Then decide:
   - **One open task clearly matches**: that is the task.
   - **It is an occurrence of a recurring task** (the match has `recurring`, or `instance_of`): pass the open occurrence, or the recurring task itself, as `task_id`.
   - **Several could match**: ask which one, listing them briefly.
   - **The match is already done**: say so; do not complete it again.
   - **Nothing matches**: this is untracked work. Use `title` instead of `task_id`, with `parent_id` if it clearly belongs under an existing task, and `motive` if you can tell it.
2. **How did it go?** If the user did not say, ask one short question: "How long did it take, and was it hard going?" If they are in a hurry, or it was trivial, skip the question.
3. **Fill in from their words:**
   - `actual_minutes`: their time in minutes ("about an hour" is 60). Leave it out if they gave none.
   - `felt_activation`: how hard it was to start (`low`, `medium`, `high`). "I kept putting it off" is `high`.
   - `felt_drain`: how tiring it was. "Wiped me out" is `high`; "easy" is `low`.
   - `reflection`: their own words on how it went, briefly.
   - `completed_at`: only if they said it was done earlier ("yesterday").
4. **File a pattern.** If you have `find_patterns`, call it with words from the title.
   - A clear match: pass its `pattern_id`. If the task already shows a `pattern`, keep it.
   - For an occurrence of a recurring task, reuse the pattern its earlier occurrences were filed under (`get_task` on the recurring task shows them). Patterns are never assigned automatically.
   - No clear match, but this is work the user will do again: ask whether it is a new kind of work, then pass `new_pattern` with a short name such as "Phone calls to book things".
   - One-off work needs no pattern.
5. **Call `complete_task` once.** If it fails with `has_open_children`, ask whether the unfinished steps are no longer needed. If they are not, repeat with `drop_open_children: true`.
6. **Handle the result.**
   - `parent_check`: the parent now has nothing open. If finishing the children clearly finished the parent, complete it too. If something obviously remains ("now submit it"), add it with `create_task` under the parent. If it is unclear, ask. Say what you did.
   - `unblocked`: tell the user what is now free to start, in a line.
   - `recurrence_check` (if present): it shows when the recurring task will come back. Leave it unless the user's words suggest another time, then set it with `update_task(snooze_until=...)`.
7. **Learn.** If the reflection says something general about this kind of work ("took twice as long as I thought", "much easier in the morning"), update the pattern's note with `save_pattern(pattern_id, learned=...)` if you have it. `learned` replaces the whole note: rewrite it to include the old points and the new one, in under 600 characters.
8. **Reply in a line or two**: what was recorded, and anything that changed.

## Do not

- Ask more than one question about how it went.
- Complete a task the user did not say was done.
- Drop open children without asking.
- Append to a pattern's `learned` note; rewrite it whole.
