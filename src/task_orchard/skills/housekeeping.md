---
name: housekeeping
description: Tidy up - merge exact duplicates, clear stale guessed deadlines, consolidate profile notes - and list what needs the user's decision.
---
# Housekeeping

Goal: keep the task list trustworthy. Fix the small things that need no judgement from the user, and hand them a short list of what does. This can run with nobody there to answer, so never wait on a question.

## Steps

1. Call `get_review_queue`. If the profile is not already in front of you, call `read_profile`.
2. **Do without asking:**
   - **Exact duplicates** (`duplicates`): call `get_task` on both. If they are the same piece of work, merge with `merge_tasks`, keeping the one with more filled in, or the older one if they are equal. If they differ in a way that matters (different parents, different deadlines), leave them for the user.
   - **Stale deadlines**: for `overdue` items with `firmness` `soft` that are more than 14 days overdue, call `get_task`. If the task's priority is `low` or `normal` and the deadline's source is not `user`, remove it with `update_task(clear=["deadline"])`.
   - **Overlapping profile notes**: if two notes say much the same thing, rewrite one to hold both with `write_profile_note`, then delete the other by writing it with blank text. Never touch the charter.
3. **Never, without asking:** drop a task, complete a task, or change a field whose source is `user`. Anything like that goes on the list in step 4.
4. **Report** in a few lines what you did, then list what needs the user's decision, most important first, at most eight items:
   - `parent_checks`: parents whose children are all closed. Ask whether each is done.
   - `overdue` items you left alone, with how long overdue.
   - `most_deferred` and `repeat_starts`: work that keeps being put off or restarted. Suggest dropping it, breaking it down, or re-committing.
   - `missed`: scheduled tasks that keep being missed. Ask whether the schedule still fits.
   - `conflicts`: a value the user set that something later disagreed with. Show `kept` and `proposed`.
   - `inbox` when it is not empty: offer to triage it.
   - `untouched`: only the oldest two or three, if they have sat for weeks.

## Do not

- Merge tasks that might be different pieces of work. When in doubt, list them.
- Re-date a deadline. Clear it or leave it; picking a new date is the user's call.
- Report sections that are empty.
