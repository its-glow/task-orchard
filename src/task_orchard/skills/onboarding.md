---
name: onboarding
description: Set up a new user in one conversation - the charter, the timezone, a first brain-dump, things that repeat, and a few notes about them.
---
# Onboarding

Goal: by the end of one conversation the user has a charter, a confirmed timezone, their open loops captured and sorted, their regular commitments repeating, and two or three profile notes. Take one step per message. Keep each message short.

## Steps

1. **Explain the idea in four sentences**, no more: Task Orchard holds their tasks as trees, breaking big things down only as far as the next step. They add tasks and say when things are done; you work out what to do next, from the facts and from what they want. A short charter, in their words, says what they want from it. Then ask for that.
2. **The charter.** Ask what they want from Task Orchard: what keeps getting crowded out, what they need kept in view, how they like to be spoken to. Draft two to four sentences in their words, show the draft, and save it with `write_profile_note` (topic `charter`) once they agree. If they would rather skip it, move on.
3. **The timezone.** Results carry `now`, which shows the time and the zone being assumed. Ask whether that is the time where they are. If it is, call `set_timezone` with that zone. If not, ask for their city and call `set_timezone` with the matching name, such as `Europe/London` or `America/Chicago`.
4. **The brain-dump.** Ask them to list everything on their mind, big or small, in any order. Capture it all with `capture_tasks` and ask nothing yet. Then follow the `triage` guide for what was captured.
5. **Things that repeat.** Ask once about fixed commitments (bills, reports, appointments) and habits they want to keep (exercise, chores). For each, call `create_task` with `recurrence`:
   - tied to the calendar: `{"mode": "scheduled", "rrule": "FREQ=MONTHLY;BYMONTHDAY=1"}`, with `due_time` if there is one;
   - anything looser: `{"mode": "flexible", "cadence": "<their words>"}`, with `gap_days` for a natural rest between times.
   Read back the `next_slots` of a scheduled one in a few words, so they can correct it.
6. **Profile notes.** From what they said, save two or three short notes with `write_profile_note`: when their energy is high or low, places or times that limit what they can do, how they like options presented. Use their words. Do not ask extra questions just to fill notes.
7. **Finish** in three or four lines: what is set up, and that they can now ask "what should I do now?".

## Do not

- Ask several things in one message, except the numbered triage questions.
- Lecture. Four sentences of explanation is the limit.
- Invent commitments or habits the user did not mention.
- Skip the timezone. Dates and schedules depend on it.
