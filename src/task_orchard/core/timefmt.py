"""Turning text into instants and instants into what a small model can use.

PHASE 1. SPEC section 3 is the contract; this summary must not contradict it.

To implement:

- parse_when(text, tz, now, day_means) -> aware UTC datetime. Accepts exactly
  the forms of SPEC 3.2: ISO date, ISO date and time (local), ISO with offset,
  "today", "tomorrow", weekday names (strictly after today), and offsets
  (+3d, +2w, +90m, +4h), any day form optionally followed by HH:MM. What a bare
  day means depends on `day_means`: end of day (23:59) for deadlines, start of
  day for snoozes, midday (or now, if that is in the future) for completions.
  Nonexistent local times move forward; ambiguous ones take the first
  occurrence. Anything else raises Invalid with the hint in SPEC 3.2.
- stamp(instant, tz, now) -> {"at": "2026-11-20T17:00", "day": "Fri",
  "rel": "in 2 days"}; `at` is local wall-clock time with no offset and is
  valid input to parse_when. The `now` stamp in every response has no `rel`.
- relative(instant, now, deadline=False) -> the `rel` phrase per the table in
  SPEC 3.3, with "overdue by ..." for a passed deadline.
- day_snooze_end(start, days, tz) -> local midnight at the start of the day
  that falls `days` x 24 hours after `start` (SPEC 3.4). `days` is a whole
  number.
"""
