"""Turning instants into what a small model can use without arithmetic.

PHASE 1. SPEC section 3. To implement:

- parse_when(text, tz, now): accept an ISO date, an ISO datetime with or
  without offset, and return aware UTC. A date alone means the end of that
  day in the user's timezone.
- local_stamp(instant, tz): "Fri 20 Nov 2026 17:00".
- relative(instant, now): "in 2 days", "3 hours ago", "overdue by 4 days".
"""
