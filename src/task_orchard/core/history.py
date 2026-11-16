"""What the model reads about the past: digests, counts and the balance.

Per-task counts (deferred, starts, last touched), pattern digests, the want /
should / must balance, the review queue and the recent-activity feed.

Everything here is aggregated on read from completions and events. Raw rows
never leave this module. SPEC section 6.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from task_orchard.core.enums import TOUCH_EVENTS, EventType, FieldSource, Level, Motive, Status
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


# The statistics of 6.1 look at a pattern's most recent completions only.
DIGEST_WINDOW = 20
DIGEST_GAPS = 5
_RATIO_SOURCES = (FieldSource.GUESS, FieldSource.USER)


def pattern_digests(session: Session, patterns: list[Pattern]) -> list[dict[str, Any]]:
    """The digest of each pattern, in the order given (SPEC 6.1).

    Members are the tasks filed under the pattern, excluding recurring tasks,
    which are containers; their instances are members. `last_done` is a
    datetime here; views turns it into a stamp.
    """
    ids = [p.id for p in patterns]
    by_status: dict[int, dict[str, int]] = {i: {} for i in ids}
    deferred: dict[int, int] = {}
    false_starts: dict[int, int] = {}
    done: dict[int, list[Completion]] = {i: [] for i in ids}
    if ids:
        member = sa.and_(Task.pattern_id.in_(ids), Task.recur_mode.is_(None))
        rows = session.execute(
            sa.select(Task.pattern_id, Task.status, sa.func.count())
            .where(member)
            .group_by(Task.pattern_id, Task.status)
        )
        for pattern_id, status, count in rows:
            by_status[pattern_id][status.value] = count
        rows = session.execute(
            sa.select(Task.pattern_id, sa.func.count())
            .join(Event, Event.task_id == Task.id)
            .where(member, Event.type == EventType.SNOOZED)
            .group_by(Task.pattern_id)
        )
        deferred = dict(rows.all())
        rows = session.execute(
            sa.select(Task.pattern_id, Task.status, sa.func.count())
            .join(Event, Event.task_id == Task.id)
            .where(
                member,
                Task.status.in_((Status.DONE, Status.DROPPED)),
                Event.type == EventType.STARTED,
            )
            .group_by(Task.id)
        )
        for pattern_id, status, starts in rows:
            wasted = starts - 1 if status is Status.DONE else starts
            false_starts[pattern_id] = false_starts.get(pattern_id, 0) + max(0, wasted)
        rows = session.execute(
            sa.select(Task.pattern_id, Completion)
            .join(Completion, Completion.task_id == Task.id)
            .where(member)
            .order_by(Completion.completed_at.desc(), Completion.id.desc())
        )
        for pattern_id, completion in rows:
            done[pattern_id].append(completion)
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "learned": p.learned,
            **{s.value: by_status[p.id].get(s.value, 0) for s in _DIGEST_STATUSES},
            "deferred": deferred.get(p.id, 0),
            "false_starts": false_starts.get(p.id, 0),
            **completion_statistics(done[p.id]),
        }
        for p in patterns
    ]


def completion_statistics(newest_first: list[Completion]) -> dict[str, Any]:
    """The digest's figures from a pattern's completions, newest first."""

    def recent(value: Callable[[Completion], Any]) -> list[Any]:
        values = (value(c) for c in newest_first)
        return [v for v in values if v is not None][:DIGEST_WINDOW]

    ratios = recent(
        lambda c: (
            c.actual_minutes / c.estimate_at_completion
            if c.actual_minutes is not None
            and c.estimate_at_completion
            and c.estimate_source_at_completion in _RATIO_SOURCES
            else None
        )
    )
    minutes = recent(lambda c: c.actual_minutes)
    times = [c.completed_at for c in newest_first[: DIGEST_GAPS + 1]]
    return {
        "actual_minutes": _number(statistics.median(minutes)) if minutes else None,
        "ratio": round(statistics.median(ratios), 1) if ratios else None,
        "ratio_samples": len(ratios),
        "felt_activation": _level(recent(lambda c: c.felt_activation)),
        "felt_drain": _level(recent(lambda c: c.felt_drain)),
        "last_done": times[0] if times else None,
        "gaps_days": [round((a - b).total_seconds() / 86_400, 1) for a, b in pairwise(times)],
    }


def _number(value: float) -> int | float:
    """One decimal place, and a whole number as an int."""
    value = round(value, 1)
    return int(value) if value == int(value) else value


def _level(levels: list[Level]) -> str | None:
    """The median level by name, ties rounding up (SPEC 6.1). round() would not:
    it rounds 2.5 to 2."""
    if not levels:
        return None
    return Level(math.floor(statistics.median(levels) + 0.5)).name.lower()


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


def recent_completions(session: Session, recurring_id: int, limit: int) -> list[datetime]:
    """When a recurring task's instances were completed, newest first."""
    return list(
        session.scalars(
            sa.select(Completion.completed_at)
            .join(Task, Task.id == Completion.task_id)
            .where(Task.parent_id == recurring_id)
            .order_by(Completion.completed_at.desc())
            .limit(limit)
        )
    )


def recurring_done(
    session: Session, recurring_ids: Iterable[int]
) -> dict[int, tuple[int, datetime]]:
    """Per recurring task: (instances done, latest completion). Only tasks with one."""
    ids = list(recurring_ids)
    if not ids:
        return {}
    rows = session.execute(
        sa.select(Task.parent_id, sa.func.count(), sa.func.max(Completion.completed_at))
        .join(Completion, Completion.task_id == Task.id)
        .where(Task.parent_id.in_(ids), Task.status == Status.DONE)
        .group_by(Task.parent_id)
    )
    return {parent: (count, latest) for parent, count, latest in rows}


# --- The balance (SPEC 6.3) ---

BALANCE_WINDOWS = {"last_7_days": 7, "last_30_days": 30}
_MOTIVES = (Motive.WANT.value, Motive.SHOULD.value, Motive.MUST.value, "unset")


def balance(session: Session, now: datetime) -> dict[str, dict[str, Any]]:
    """Minutes and completions by motive at completion, for both windows.
    Unlike everything else in a response, zeros are kept."""
    oldest = now - timedelta(days=max(BALANCE_WINDOWS.values()))
    rows = session.execute(
        sa.select(
            Completion.completed_at,
            Completion.motive_at_completion,
            Completion.actual_minutes,
            Completion.estimate_at_completion,
        ).where(Completion.completed_at >= oldest, Completion.completed_at <= now)
    ).all()
    out = {}
    for name, days in BALANCE_WINDOWS.items():
        start = now - timedelta(days=days)
        minutes = dict.fromkeys(_MOTIVES, 0)
        completions = dict.fromkeys(_MOTIVES, 0)
        estimated = unknown = 0
        for completed_at, motive, actual, estimate in rows:
            if completed_at < start:
                continue
            group = "unset" if motive is None else motive.value
            completions[group] += 1
            if actual is not None:
                minutes[group] += actual
            elif estimate is not None:
                minutes[group] += estimate
                estimated += 1
            else:
                unknown += 1
        out[name] = {
            "minutes": minutes,
            "completions": completions,
            "estimated": estimated,
            "unknown_duration": unknown,
        }
    return out
