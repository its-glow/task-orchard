"""What the model reads about the past: digests, counts and the balance.

PHASE 3: per-task counts (deferred, starts, last touched) and the pattern
digest's stored fields and status counts.
PHASE 5: the digest's statistics, the want / should / must balance, the review
queue and the recent-activity feed.

Everything here is aggregated on read from completions and events. Raw rows
never leave this module. SPEC section 6.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from task_orchard.core.enums import TOUCH_EVENTS, EventType, Status
from task_orchard.db.models import Completion, Event, Pattern, Task

# An `updated` event is a touch only when some change in it was written with
# source `user` (SPEC 5.6). Checked in SQL so payloads never leave the database.
_USER_CHANGE = sa.text(
    "EXISTS (SELECT 1 FROM json_each(events.payload, '$.changes') AS c "
    "WHERE json_extract(c.value, '$.source') = 'user')"
)


@dataclass(frozen=True)
class Counts:
    """The per-task facts of SPEC 5.6. `last_touched` is None when no event
    counts; the caller falls back to the task's `created_at`."""

    deferred: int = 0
    starts: int = 0
    last_touched: datetime | None = None


def task_counts(session: Session, task_ids: Iterable[int]) -> dict[int, Counts]:
    """Counts for each of `task_ids`, in two grouped queries."""
    ids = list(task_ids)
    if not ids:
        return {}
    deferred: dict[int, int] = {}
    starts: dict[int, int] = {}
    touched: dict[int, datetime] = {}

    def touch(task_id: int, at: datetime) -> None:
        if task_id not in touched or at > touched[task_id]:
            touched[task_id] = at

    kinds = {*TOUCH_EVENTS, EventType.SNOOZED, EventType.STARTED}
    rows = session.execute(
        sa.select(Event.task_id, Event.type, sa.func.count(), sa.func.max(Event.at))
        .where(Event.task_id.in_(ids), Event.type.in_(kinds))
        .group_by(Event.task_id, Event.type)
    )
    for task_id, kind, count, latest in rows:
        if kind is EventType.SNOOZED:
            deferred[task_id] = count
        elif kind is EventType.STARTED:
            starts[task_id] = count
        if kind in TOUCH_EVENTS:
            touch(task_id, latest)

    rows = session.execute(
        sa.select(Event.task_id, sa.func.max(Event.at))
        .where(Event.task_id.in_(ids), Event.type == EventType.UPDATED, _USER_CHANGE)
        .group_by(Event.task_id)
    )
    for task_id, latest in rows:
        touch(task_id, latest)

    return {i: Counts(deferred.get(i, 0), starts.get(i, 0), touched.get(i)) for i in ids}


def task_starts(session: Session, task_id: int) -> int:
    return task_counts(session, [task_id])[task_id].starts


# --- Pattern digests (SPEC 6.1) ---


def pattern_digests(session: Session, patterns: list[Pattern]) -> list[dict[str, Any]]:
    """The digest of each pattern, in the order given.

    Phase 3: the stored fields and member counts by status. Members exclude
    recurring tasks, which are containers; their instances are members.
    """
    ids = [p.id for p in patterns]
    by_status: dict[int, dict[str, int]] = {i: {} for i in ids}
    if ids:
        rows = session.execute(
            sa.select(Task.pattern_id, Task.status, sa.func.count())
            .where(Task.pattern_id.in_(ids), Task.recur_mode.is_(None))
            .group_by(Task.pattern_id, Task.status)
        )
        for pattern_id, status, count in rows:
            by_status[pattern_id][status.value] = count
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "learned": p.learned,
            **{s.value: by_status[p.id].get(s.value, 0) for s in _DIGEST_STATUSES},
        }
        for p in patterns
    ]


_DIGEST_STATUSES = (Status.DONE, Status.DROPPED, Status.MISSED, Status.OPEN)


def latest_completion_by_pattern(session: Session) -> dict[int, datetime]:
    """When each pattern was last used: its members' latest completion."""
    rows = session.execute(
        sa.select(Task.pattern_id, sa.func.max(Completion.completed_at))
        .join(Completion, Completion.task_id == Task.id)
        .where(Task.pattern_id.is_not(None), Task.recur_mode.is_(None))
        .group_by(Task.pattern_id)
    )
    return dict(rows.all())
