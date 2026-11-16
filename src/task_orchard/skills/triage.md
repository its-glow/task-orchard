---
name: triage
description: Sort the inbox. Fill in each captured task's fields by guessing, ask the user as little as possible, and say what you assumed.
---
# Triage

Goal: every task in the inbox gets its fields filled in and leaves the inbox, with at most one question per task, asked in one batch.

## Steps

1. If the user's profile is not already in front of you, call `read_profile`. The charter and notes are your best evidence about the user.
2. Call `search_tasks` with `inbox_only: true` and `limit: 30`. These are the tasks to triage. If there are none, say so and stop. If `total` is more than the rows returned, finish this batch, then search again: triaged tasks leave the inbox, so the next search returns the rest.
3. For each task, **guess first**. Work out from its title, its parent (`get_task` if it has one) and the profile:
   - `priority`, `deadline` and `firmness`, `estimate_minutes`, `activation`, `drain`, `motive`, `conditions`, `achievable`.
   - If you have a `find_patterns` tool, call it with words from the title. When a pattern clearly matches, take its figures and set `pattern_id`.
   - Leave a field out when you have no real basis for it. Never fill in a middle value just to have one.
   - `achievable` is `yes` only for one clear step of a few hours or less. Something bigger is `no`.
   - `motive`: `want` if the user wants to, `should` if self-imposed, `must` if someone else requires it.
4. **Decide what to ask.** Ask only when the answer is both unclear and matters: a possible deadline on something that sounds date-bound is worth asking about; how tiring it is to buy milk is not.
   - At most one question per task. Put all the questions in **one message**, numbered.
   - Prefer a question that settles many tasks ("Are phone calls usually hard to start for you?"). Save the answer with `write_profile_note` so it is never asked again.
   - If nothing needs asking, do not ask.
5. **Write each task** with one `update_task` call. This also takes it out of the inbox.
   - Mark where each value came from in `sources`: `"user"` for what the user said, `"pattern"` for what you took from a pattern. Leave your own guesses out of `sources`; they are recorded as guesses.
   - If the result has `skipped`, the user had set those fields before. Leave them unless the user has just asked for the change.
6. **Duplicates.** If `capture_tasks` reported `possible_duplicates`, or two inbox tasks look like the same thing, say so and ask whether to keep both.
7. **Recurring intent** ("twice a week", "every Friday"): if `update_task` accepts `recurrence`, convert the task. For "the last day of the month" use `BYMONTHDAY=-1`. Otherwise note the intent in the task's notes and tell the user it will repeat later.
8. **Instances of recurring tasks** (rows with `instance_of`) also land in the inbox. Fill them like any task, using the parent and its previous occurrence (`get_task` on the parent shows its recent children). Do not ask the user about them.
9. **Finish** by telling the user, in two or three lines, what you assumed: "I've put the desk down as low priority, about an hour, no deadline." A correction is cheaper for them than a question.

## Do not

- Ask questions while the user is capturing. Triage comes later.
- Ask more than one question about any task.
- Overwrite something the user set, unless they just asked you to.
