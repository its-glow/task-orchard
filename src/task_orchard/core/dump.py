"""Every task as flat rows, for `task-orchard dump` (SPEC 10.4).

A tester's view, not a tool: the stored fields, then the derived facts the
model reasons from, computed now from one snapshot. Every value is a string,
so the rows go straight into a CSV writer. Description and notes are left out:
they are free text that may run over several lines, and one line per task is
the point.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from task_orchard.core import timefmt, views
from task_orchard.core.derive import Effective, Snapshot
from task_orchard.core.enums import TRACKED_FIELDS, Status
from task_orchard.db.models import Task

STORED = [
    "id",
    "parent_id",
    "title",
    "status",
    "inbox",
    "priority",
    "deadline",
    "firmness",
    "estimate_minutes",
    "activation",
    "drain",
    "motive",
    "conditions",
    "achievable",
    "snooze_until",
    "pattern_id",
    "recurrence",
    "rule",
    "gap_days",
    "created",
    "closed",
]
COMPUTED = [
    "blocked",
    "dormant",
    "candidate",
    "overdue",
    "at_risk",
    "effective_priority",
    "effective_deadline",
    "effective_firmness",
    "effective_motive",
]


def columns(*, sources: bool) -> list[str]:
    extra = [f"{field}_source" for field in TRACKED_FIELDS] if sources else []
    return STORED + COMPUTED + extra


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(views.wire_name(value))


def _time(value: datetime | None, tz: ZoneInfo) -> str:
    return "" if value is None else timefmt.at_text(value, tz)


def _effective(found: Effective | None) -> Any:
    return None if found is None else found.value


def rows(snap: Snapshot, tz: ZoneInfo, *, open_only: bool, sources: bool) -> list[dict[str, str]]:
    query = sa.select(Task).order_by(Task.id)
    if open_only:
        query = query.where(Task.status == Status.OPEN)
    return [_row(snap, t, tz, sources) for t in snap.session.scalars(query)]


def _row(snap: Snapshot, task: Task, tz: ZoneInfo, sources: bool) -> dict[str, str]:
    rule = task.rrule if task.rrule is not None else task.cadence
    row = {
        "id": _text(task.id),
        "parent_id": _text(task.parent_id),
        "title": " ".join(task.title.splitlines()),
        "status": _text(task.status),
        "inbox": _text(not task.triaged),
        "priority": _text(task.priority),
        "deadline": _time(task.deadline, tz),
        "firmness": _text(task.deadline_firmness),
        "estimate_minutes": _text(task.estimate_minutes),
        "activation": _text(task.activation),
        "drain": _text(task.drain),
        "motive": _text(task.motive),
        "conditions": ";".join(task.conditions or []),
        "achievable": _text(task.achievable),
        "snooze_until": _time(task.snooze_until, tz),
        "pattern_id": _text(task.pattern_id),
        "recurrence": _text(task.recur_mode),
        "rule": rule or "",
        "gap_days": _text(task.gap_days),
        "created": _time(task.created_at, tz),
        "closed": _time(task.closed_at, tz),
    }
    computed = dict.fromkeys(COMPUTED, "")
    if task.status is Status.OPEN:
        due = snap.effective_due(task)
        computed.update(
            blocked=_text(snap.blocked(task)),
            dormant=_text(snap.dormant(task)),
            candidate=_text(snap.candidate(task)),
            overdue=_text(snap.overdue(task)),
            at_risk=_text(snap.at_risk(task)),
            effective_priority=_text(_effective(snap.effective_priority(task))),
            effective_deadline=_time(_effective(due), tz),
            effective_firmness=_text(None if due is None else due.firmness),
            effective_motive=_text(_effective(snap.effective_motive(task))),
        )
    row.update(computed)
    if sources:
        recorded = task.field_sources or {}
        for field in TRACKED_FIELDS:
            row[f"{field}_source"] = recorded.get(field, "")
    return row
