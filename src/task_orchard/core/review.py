"""The review queue and recent activity (SPEC 6.5, 6.6).

Facts for housekeeping and the weekly review. The server sorts and cuts; it
applies no thresholds beyond "greater than zero", and the agent decides what is
worth raising. Items are slim rows, so the whole queue fits the response budget.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session

from task_orchard.core import history, timefmt, views
from task_orchard.core.derive import Snapshot
from task_orchard.core.enums import EventType, RecurMode, Status
from task_orchard.db.models import Completion, Event, Task

SECTION_ITEMS = 10
INBOX_ITEMS = 30
CONFLICT_DAYS = 30
MISSED_DAYS = 30
ACTIVITY_COMPLETED = 30
ACTIVITY_CLOSED = 10
ACTIVITY_DAYS_MAX = 31
ACTIVITY_DEFAULT_DAYS = 1

_TITLE_PUNCTUATION = re.compile(r"^[\W_]+|[\W_]+$")


def normalise_title(title: str) -> str:
    """For duplicate detection: case, spacing and surrounding punctuation ignored."""
    return _TITLE_PUNCTUATION.sub("", " ".join(title.casefold().split()))


def _section(items: list[Any], limit: int = SECTION_ITEMS) -> dict[str, Any]:
    return {"items": items[:limit], "total": len(items)}


def _slim(snap: Snapshot, task: Task, **extra: Any) -> dict[str, Any]:
    return views.compact(
        {
            "id": task.id,
            "title": views.truncate(task.title, views.ROW_TITLE_CHARS),
            "path": views.path(snap, task),
            **extra,
        },
        keep=("id", "title"),
    )


def _days(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds() // 86_400)


def review_queue(snap: Snapshot, tz: ZoneInfo) -> dict[str, Any]:
    s, now = snap.session, snap.now
    tasks = snap.open_tasks()
    leaves = [t for t in tasks if snap.is_leaf(t)]

    inbox = sorted((t for t in tasks if not t.triaged), key=lambda t: (t.created_at, t.id))
    with_children = set(
        s.scalars(sa.select(Task.parent_id).where(Task.parent_id.in_(snap.open_ids)).distinct())
    )
    parent_checks = [
        t
        for t in tasks
        if t.id in with_children and not t.is_recurring and not snap.has_open_children(t)
    ]
    overdue = sorted(
        (t for t in leaves if t.deadline is not None and t.deadline < now),
        key=lambda t: (t.deadline, t.id),
    )
    deferred = sorted(
        (t for t in tasks if snap.counts(t).deferred > 0),
        key=lambda t: (-snap.counts(t).deferred, t.id),
    )
    restarted = sorted(
        (t for t in tasks if snap.counts(t).starts > 1),
        key=lambda t: (-snap.counts(t).starts, t.id),
    )
    untouched = sorted(
        (t for t in leaves if t.triaged and not snap.dormant(t)),
        key=lambda t: (snap.last_touched(t), t.id),
    )

    return {
        "inbox": _section(
            [
                _slim(snap, t, instance_of=t.parent_id if snap.is_instance(t) else None)
                for t in inbox
            ],
            INBOX_ITEMS,
        ),
        "parent_checks": _section([_slim(snap, t) for t in parent_checks]),
        "overdue": _section(
            [
                _slim(
                    snap,
                    t,
                    overdue_days=_days(now, t.deadline),
                    firmness=t.deadline_firmness.value,
                )
                for t in overdue
            ]
        ),
        "most_deferred": _section(
            [_slim(snap, t, deferred=snap.counts(t).deferred) for t in deferred]
        ),
        "repeat_starts": _section(
            [_slim(snap, t, starts=snap.counts(t).starts) for t in restarted]
        ),
        "untouched": _section(
            [_slim(snap, t, untouched_days=_days(now, snap.last_touched(t))) for t in untouched]
        ),
        "duplicates": _section(_duplicates(snap, tasks)),
        "conflicts": _section(_conflicts(snap)),
        "missed": _section(_missed(s, now)),
    }


def _family(snap: Snapshot, a: Task, b: Task) -> bool:
    """A recurring task and its instance, or two instances of one recurring task."""
    if a.parent_id == b.id:
        return b.is_recurring
    if b.parent_id == a.id:
        return a.is_recurring
    return a.parent_id is not None and a.parent_id == b.parent_id and snap.is_instance(a)


def _duplicates(snap: Snapshot, tasks: list[Task]) -> list[dict[str, Any]]:
    groups: dict[str, list[Task]] = {}
    for t in tasks:
        groups.setdefault(normalise_title(t.title), []).append(t)
    pairs = []
    for group in groups.values():
        for a, b in combinations(group, 2):
            if not _family(snap, a, b):
                pairs.append((a, b))
    pairs.sort(key=lambda p: (p[0].id, p[1].id))
    return [{"a": views.ref(a), "b": views.ref(b)} for a, b in pairs]


def _conflicts(snap: Snapshot) -> list[dict[str, Any]]:
    since = snap.now - timedelta(days=CONFLICT_DAYS)
    rows = snap.session.scalars(
        sa.select(Event)
        .where(
            Event.type == EventType.CONFLICT,
            Event.at >= since,
            Event.task_id.in_(snap.open_ids),
        )
        .order_by(Event.at.desc(), Event.id.desc())
    )
    return [
        {
            "task": views.ref(snap.task(e.task_id)),
            "field": e.payload.get("field"),
            "kept": e.payload.get("kept"),
            "proposed": e.payload.get("proposed"),
            "source": e.payload.get("source"),
            "when": timefmt.relative(e.at, snap.now),
        }
        for e in rows
    ]


def _missed(session: Session, now: datetime) -> list[dict[str, Any]]:
    since = now - timedelta(days=MISSED_DAYS)
    missed = dict(
        session.execute(
            sa.select(Task.parent_id, sa.func.count())
            .join(Event, Event.task_id == Task.id)
            .where(Event.type == EventType.MISSED, Event.at >= since)
            .group_by(Task.parent_id)
        ).all()
    )
    if not missed:
        return []
    done = dict(
        session.execute(
            sa.select(Task.parent_id, sa.func.count())
            .join(Completion, Completion.task_id == Task.id)
            .where(Task.parent_id.in_(missed), Completion.completed_at >= since)
            .group_by(Task.parent_id)
        ).all()
    )
    recurring = {
        t.id: t
        for t in session.scalars(
            sa.select(Task).where(Task.id.in_(missed), Task.recur_mode == RecurMode.SCHEDULED)
        )
    }
    order = sorted(recurring, key=lambda i: (-missed[i], i))
    return [
        {"recurring": views.ref(recurring[i]), "missed": missed[i], "done": done.get(i, 0)}
        for i in order
    ]


# --- Recent activity (6.6) ---


def recent_activity(session: Session, now: datetime, days: int) -> dict[str, Any]:
    since = now - timedelta(days=days)
    completed = session.execute(
        sa.select(Task, Completion)
        .join(Completion, Completion.task_id == Task.id)
        .where(Completion.completed_at >= since, Completion.completed_at <= now)
        .order_by(Completion.completed_at.desc(), Completion.id.desc())
    ).all()
    dropped = _closed(session, since, Status.DROPPED, (EventType.DROPPED, EventType.MERGED))
    missed = _closed(session, since, Status.MISSED, (EventType.MISSED,))
    counts = dict(
        session.execute(
            sa.select(Event.type, sa.func.count())
            .where(
                Event.at >= since,
                Event.type.in_((EventType.CREATED, EventType.SNOOZED, EventType.STARTED)),
            )
            .group_by(Event.type)
        ).all()
    )
    return {
        "completed": [
            views.compact(
                {
                    "id": t.id,
                    "title": views.truncate(t.title, views.ROW_TITLE_CHARS),
                    "actual_minutes": c.actual_minutes,
                    "motive": views.wire_name(c.motive_at_completion),
                    "when": timefmt.relative(c.completed_at, now),
                },
                keep=("id", "title", "when"),
            )
            for t, c in completed[:ACTIVITY_COMPLETED]
        ],
        "completed_count": len(completed),
        "dropped": [_closed_item(t, e, now) for t, e in dropped[:ACTIVITY_CLOSED]],
        "dropped_count": len(dropped),
        "missed": [_closed_item(t, e, now) for t, e in missed[:ACTIVITY_CLOSED]],
        "missed_count": len(missed),
        "created_count": counts.get(EventType.CREATED, 0),
        "deferred_count": counts.get(EventType.SNOOZED, 0),
        "started_count": counts.get(EventType.STARTED, 0),
        "balance": history.balance(session, now),
    }


def _closed(
    session: Session, since: datetime, status: Status, kinds: tuple[EventType, ...]
) -> list[tuple[Task, Event]]:
    """Tasks still `status` whose closing event falls in the window, newest first."""
    rows = session.execute(
        sa.select(Task, Event)
        .join(Event, Event.task_id == Task.id)
        .where(Task.status == status, Event.type.in_(kinds), Event.at >= since)
        .order_by(Event.at.desc(), Event.id.desc())
    ).all()
    seen: set[int] = set()
    out = []
    for task, event in rows:
        if task.id not in seen:  # a task dropped, reopened and dropped again: the latest
            seen.add(task.id)
            out.append((task, event))
    return out


def _closed_item(task: Task, event: Event, now: datetime) -> dict[str, Any]:
    reason = event.payload.get("reason")
    if event.type is EventType.MERGED:
        reason = f"merged into #{event.payload.get('into')}"
    return views.compact(
        {
            "id": task.id,
            "title": views.truncate(task.title, views.ROW_TITLE_CHARS),
            "when": timefmt.relative(event.at, now),
            "reason": reason,
        }
    )
