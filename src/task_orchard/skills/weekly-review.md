---
name: weekly-review
description: Review the week with the user - what got done, the balance against the charter, and a decision on each stale item.
---
# Weekly review

Goal: with the user, look back at the week and leave the list in a state they trust: done work acknowledged, the balance compared with the charter, and every stale item dropped, reshaped or recommitted. This is where larger questions belong.

## Steps

1. Call `get_recent_activity` with `days: 7` and `get_review_queue`. If the profile is not already in front of you, call `read_profile`.
2. **Look back**, in three or four lines: how many things were completed and a few notable ones by title, anything dropped or missed, and the balance. Compare the `last_7_days` minutes for `want`, `should` and `must` with what the charter asks for, in plain words ("almost all must this week, and no time on the game").
3. **Stale items.** Gather the top few from `most_deferred`, `repeat_starts`, `overdue` and the oldest of `untouched`, at most six in all. Present them in one numbered message. For each, offer three choices:
   - **drop** it (`drop_tasks` with the user's reason);
   - **reshape** it: break it down with the `subdivide` guide, or propose a smaller first step;
   - **recommit**: a new date (`update_task` with a deadline, source `user`) or a snooze until a real date (`snooze_task`).
   Let the user answer for all of them at once. Act on each answer.
4. **Missed recurrences** (`missed`): ask whether the schedule still fits. Change it with `update_task(recurrence=...)`, pause it with `snooze_task`, or drop it.
5. **Conflicts**: show each one's `kept` and `proposed` values and ask which is right. Write the answer with `update_task`, with source `user`.
6. **Parent checks**: ask whether each parent with nothing left open is done; complete it with `complete_task` if so.
7. **The charter.** If the balance has been far from the charter, ask whether the charter or the plan should change. Save a changed charter with `write_profile_note`.
8. **Close** with two or three lines on what changed. If the review taught something general about the user, save it as a profile note.

## Do not

- Decide stale items for the user. Offer the three choices and wait.
- Walk every item in the queue. Six stale items is plenty for one review.
- Drop or complete anything the user did not choose to.
