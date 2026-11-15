"""The candidate shortlist and the critical list.

SPEC sections 5.7 and 5.8. The order is deliberately plain, with no weighted
score. Final ranking is the agent's job; this module decides what makes the
cut, attaches the facts, and says what it left out. Further filters are
welcome if they meet the test in SPEC section 1: simple, visible in the
response, and adjustable.

The three constants below are used by derive for "at risk" and live here and
nowhere else. This module imports derive only for type checking, so that
derive can import them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from task_orchard.core import views
from task_orchard.core.enums import Level, Priority, Status

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from task_orchard.core.derive import Snapshot
    from task_orchard.db.models import Task

AT_RISK_MIN_HOURS = 24
AT_RISK_WORK_FACTOR = 3
DUE_SOON_HOURS = 72

GROUPS = ("pressing", "due_soon", "other")
CRITICAL_MAX = 30
CRITICAL_BLOCKERS = 5
_NEVER = datetime.max.replace(tzinfo=None)


@dataclass
class Filters:
    """The narrowing filters of 5.7 step 2, in the order they are applied.
    None means the caller did not supply it."""

    under: frozenset[int] | None = None
    min_priority: Priority | None = None
    due_within: timedelta | None = None
    minutes_available: int | None = None
    exclude_conditions: frozenset[str] | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("outside", self.under),
            ("below_priority", self.min_priority),
            ("not_due", self.due_within),
            ("too_long", self.minutes_available),
            ("conditions", self.exclude_conditions),
        ):
            if value is not None:
                self.counts[name] = 0

    def reason(self, snap: Snapshot, task: Task) -> str | None:
        """The first filter that removes the task, or None if it stays."""
        if self.under is not None and task.id not in self.under:
            return "outside"
        if self.min_priority is not None and snap.priority_for_order(task) < self.min_priority:
            return "below_priority"
        if self.due_within is not None:
            due = snap.effective_due(task)
            if due is None or due.value - snap.now > self.due_within:
                return "not_due"
        if (
            self.minutes_available is not None
            and task.estimate_minutes is not None
            and task.estimate_minutes > self.minutes_available
        ):
            return "too_long"
        if self.exclude_conditions is not None and self.exclude_conditions & set(
            task.conditions or []
        ):
            return "conditions"
        return None


def group_of(snap: Snapshot, task: Task) -> str:
    if snap.overdue(task, hard=True) or snap.at_risk(task, hard=True):
        return "pressing"
    due = snap.effective_due(task)
    if due is not None and due.value - snap.now <= timedelta(hours=DUE_SOON_HOURS):
        return "due_soon"  # also covers overdue on a soft deadline
    return "other"


def _due_key(snap: Snapshot, task: Task) -> tuple[int, datetime]:
    due = snap.effective_due(task)
    return (1, _NEVER) if due is None else (0, due.value.replace(tzinfo=None))


def _order(snap: Snapshot, task: Task, group: str) -> tuple[Any, ...]:
    return (
        GROUPS.index(group),
        -snap.priority_for_order(task),
        _due_key(snap, task),
        task.created_at,
        task.id,
    )


def inbox_count(snap: Snapshot) -> int:
    return sum(1 for t in snap.open_tasks() if not t.triaged)


def candidates(
    snap: Snapshot,
    tz: ZoneInfo,
    *,
    limit: int,
    filters: Filters,
    energy: Level | None,
    reserve_wants: int,
) -> dict[str, Any]:
    """The shortlist of SPEC 5.7, as the payload of get_candidates."""
    pool = [t for t in snap.open_tasks() if snap.candidate(t)]
    conditions_in_pool = sorted({c for t in pool for c in (t.conditions or [])})

    kept: list[Task] = []
    for task in pool:
        reason = filters.reason(snap, task)
        if reason is None:
            kept.append(task)
        else:
            filters.counts[reason] += 1

    group = {t.id: group_of(snap, t) for t in kept}
    ordered = sorted(kept, key=lambda t: _order(snap, t, group[t.id]))
    shown, cut = ordered[:limit], ordered[limit:]

    # Reserve places for wants (step 6).
    reserve = min(reserve_wants, limit // 3)
    need = reserve - sum(1 for t in shown if snap.is_want(t))
    reserved: set[int] = set()
    while need > 0:
        incoming = next((t for t in cut if snap.is_want(t)), None)
        outgoing = next(
            (t for t in reversed(shown) if group[t.id] == "other" and not snap.is_want(t)), None
        )
        if incoming is None or outgoing is None:
            break
        shown.remove(outgoing)
        cut.remove(incoming)
        shown.append(incoming)
        cut.append(outgoing)
        reserved.add(incoming.id)
        need -= 1
    shown.sort(key=lambda t: _order(snap, t, group[t.id]))

    rows = []
    for task in shown:
        row = views.task_row(snap, task, tz)
        row["group"] = group[task.id]
        if task.id in reserved:
            row["reserved"] = True
        if energy is not None and task.drain is not None and task.drain > energy:
            row["over_energy"] = True
        rows.append(row)

    cut_counts = dict.fromkeys(GROUPS, 0)
    for task in cut:
        cut_counts[group[task.id]] += 1
    return {
        "candidates": rows,
        "pool": {"total": len(kept), "shown": len(rows), "cut": cut_counts},
        "filtered": filters.counts,
        "conditions_in_pool": conditions_in_pool,
        "inbox": inbox_count(snap),
    }


def critical(snap: Snapshot, tz: ZoneInfo, *, horizon: timedelta) -> dict[str, Any]:
    """The critical list of SPEC 5.8, as the payload of get_critical."""
    found: list[Task] = []
    for task in snap.open_tasks():
        if task.status is not Status.OPEN or not snap.is_leaf(task):
            continue
        due = snap.effective_due(task)
        if (
            snap.overdue(task)
            or snap.at_risk(task)
            or (due is not None and due.value - snap.now <= horizon)
            or snap.priority_for_order(task) is Priority.CRITICAL
        ):
            found.append(task)
    found.sort(key=lambda t: (_due_key(snap, t), -snap.priority_for_order(t), t.id))
    rows = [
        views.task_row(snap, t, tz, blockers=CRITICAL_BLOCKERS, blocker_via=True)
        for t in found[:CRITICAL_MAX]
    ]
    return {
        "critical": rows,
        "omitted": max(0, len(found) - CRITICAL_MAX),
        "inbox": inbox_count(snap),
    }
