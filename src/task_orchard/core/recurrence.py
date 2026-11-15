"""Recurrence: the scheduled spawner and the flexible completion step.

PHASE 4. Until then roll_forward() is a deliberate no-op so that the service's
transaction wrapper can already call it on every request.

To implement (SPEC section 7):

- next_slot(task, after, tz): the first RRULE slot strictly after `after`,
  evaluated in naive local wall-clock time from Task.recur_dtstart and
  returned as aware UTC.
- validate_rrule(text): parse with dateutil; reject sub-daily frequencies.
- roll_forward(session, now, tz): resume expired pauses, spawn due instances,
  enforce the two-live-instances cap. Idempotent.
- after_flexible_completion(...): apply gap_days and build the recurrence check.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session


def roll_forward(session: Session, now: datetime, tz: ZoneInfo) -> None:
    """Apply everything the clock has made due. Runs at the start of every request."""
    return None
