"""Derived state, computed on read and never stored.

- Predicates (5.3): open, dormant, blocked, has_open_children, is_instance,
  is_leaf, candidate, and the row `state` flags.
- Effective values (5.4): priority, deadline (any and hard passes) and motive,
  each with where it came from, by memoised recursion along the edges of 5.1.
  Only open tasks take part; a closed task shows its own values.
- Remaining work, overdue and at risk (5.5).

A Snapshot loads every open task and every blocker link once per request, and
answers each question from that (SPEC section 5). Tasks outside the open set
(a closed ancestor, a closed task being shown) are fetched on demand. Nothing
is cached between requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from task_orchard.core import history
from task_orchard.core.enums import Firmness, Motive, Priority, RecurMode, Status
from task_orchard.core.shortlist import AT_RISK_MIN_HOURS, AT_RISK_WORK_FACTOR
from task_orchard.db.models import Pattern, Task, TaskBlock

PARENT = "parent"
BLOCKS = "blocks"


@dataclass(frozen=True)
class Effective:
    """An effective value and where it came from.

    `via` is None when the value is the task's own; otherwise it is the parent,
    or the explicitly blocked task that supplied it (even when the value came
    up through that task's descendant), and `how` says which.
    """

    value: Any
    via: int | None = None
    how: str | None = None
    firmness: Firmness | None = None  # deadlines only


class Snapshot:
    def __init__(self, session: Session, now: datetime) -> None:
        self.session = session
        self.now = now
        session.flush()
        self._tasks: dict[int, Task] = {
            t.id: t for t in session.scalars(sa.select(Task).where(Task.status == Status.OPEN))
        }
        self.open_ids: frozenset[int] = frozenset(self._tasks)

        self.blockers_of: dict[int, list[int]] = {}  # blocked -> blockers (any status)
        self.blocking: dict[int, list[int]] = {}  # blocker -> blocked (any status)
        links = session.execute(
            sa.select(TaskBlock.blocker_id, TaskBlock.blocked_id).order_by(
                TaskBlock.blocker_id, TaskBlock.blocked_id
            )
        )
        for blocker, blocked in links:
            self.blockers_of.setdefault(blocked, []).append(blocker)
            self.blocking.setdefault(blocker, []).append(blocked)

        self.open_children: dict[int, list[int]] = {}
        for t in sorted(self._tasks.values(), key=lambda t: t.id):
            if t.parent_id is not None:
                self.open_children.setdefault(t.parent_id, []).append(t.id)

        self._priority: dict[int, Effective | None] = {}
        self._due: dict[tuple[int, bool], Effective | None] = {}
        self._remaining: dict[int, int] = {}
        self._counts: dict[int, history.Counts] | None = None
        self._patterns: dict[int, Pattern] | None = None

    # --- Access ---

    def task(self, task_id: int) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            task = self.session.get(Task, task_id)
            if task is None:  # pragma: no cover - callers pass ids they loaded
                raise KeyError(task_id)
            self._tasks[task_id] = task
        return task

    def chain(self, task: Task) -> list[Task]:
        """The task, then its parent, grandparent, ... up to the root."""
        out = [task]
        seen = {task.id}
        while out[-1].parent_id is not None and out[-1].parent_id not in seen:
            parent = self.task(out[-1].parent_id)
            out.append(parent)
            seen.add(parent.id)
        return out

    def ancestors(self, task: Task) -> list[Task]:
        return self.chain(task)[1:]

    def open_tasks(self) -> list[Task]:
        """Every open task, in id order."""
        return [self._tasks[i] for i in sorted(self.open_ids)]

    def is_open(self, task_id: int) -> bool:
        return task_id in self.open_ids

    def counts(self, task: Task) -> history.Counts:
        """Per-task counts (SPEC 5.6), loaded for every open task at once."""
        if self._counts is None:
            self._counts = history.task_counts(self.session, self.open_ids)
        if task.id not in self._counts:
            self._counts.update(history.task_counts(self.session, [task.id]))
        return self._counts[task.id]

    def last_touched(self, task: Task) -> datetime:
        return self.counts(task).last_touched or task.created_at

    def pattern(self, pattern_id: int) -> Pattern:
        if self._patterns is None:
            self._patterns = {p.id: p for p in self.session.scalars(sa.select(Pattern))}
        return self._patterns[pattern_id]

    # --- Predicates (SPEC 5.3) ---

    def dormant(self, task: Task) -> bool:
        return any(
            t.snooze_until is not None and t.snooze_until > self.now for t in self.chain(task)
        )

    def dormant_by_own_snooze(self, task: Task) -> bool:
        return task.snooze_until is not None and task.snooze_until > self.now

    def effective_blockers(self, task: Task) -> list[tuple[int, int | None]]:
        """Open blockers of the task or of any ancestor: (blocker id, via ancestor id).

        `via` is None for the task's own blockers. A blocker reached through
        more than one ancestor is listed once, by its nearest route.
        """
        out: list[tuple[int, int | None]] = []
        seen: set[int] = set()
        for depth, node in enumerate(self.chain(task)):
            for blocker in self.blockers_of.get(node.id, []):
                if blocker in seen or not self.is_open(blocker):
                    continue
                seen.add(blocker)
                out.append((blocker, None if depth == 0 else node.id))
        return out

    def blocked(self, task: Task) -> bool:
        return bool(self.effective_blockers(task))

    def has_open_children(self, task: Task) -> bool:
        return bool(self.open_children.get(task.id))

    def is_instance(self, task: Task) -> bool:
        return task.parent_id is not None and self.task(task.parent_id).is_recurring

    def is_leaf(self, task: Task) -> bool:
        """Open, not recurring, and no open children (the one definition, SPEC 2)."""
        return (
            task.status is Status.OPEN
            and not task.is_recurring
            and not self.has_open_children(task)
        )

    def candidate(self, task: Task) -> bool:
        if task.status is not Status.OPEN or not task.triaged:
            return False
        if self.dormant(task) or self.blocked(task) or self.has_open_children(task):
            return False
        return task.recur_mode in (None, RecurMode.FLEXIBLE)

    def unblocks(self, task: Task) -> int:
        """How many open tasks this one explicitly blocks."""
        return sum(1 for b in self.blocking.get(task.id, []) if self.is_open(b))

    # --- Effective values (SPEC 5.4) ---

    def open_descendants(self, task_id: int) -> list[int]:
        out: list[int] = []
        stack = list(reversed(self.open_children.get(task_id, [])))
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(reversed(self.open_children.get(node, [])))
        return out

    def effectively_blocks(self, task: Task) -> list[tuple[int, int]]:
        """Open tasks this one effectively blocks, as (task id, explicitly blocked id).

        The explicitly blocked tasks first, each followed by its open descendants.
        """
        out: list[tuple[int, int]] = []
        for blocked in self.blocking.get(task.id, []):
            if not self.is_open(blocked):
                continue
            out.append((blocked, blocked))
            out.extend((d, blocked) for d in self.open_descendants(blocked))
        return out

    def _open_parent(self, task: Task) -> Task | None:
        if task.parent_id is None or not self.is_open(task.parent_id):
            return None
        return self.task(task.parent_id)

    def effective_priority(self, task: Task) -> Effective | None:
        """The highest of own, the parent's, and every effectively blocked task's.

        Ties prefer own, then parent, then blocked tasks in id order. None when
        nothing sets one; ordering and filtering then treat it as normal.
        """
        if task.status is not Status.OPEN:
            return None if task.priority is None else Effective(task.priority)
        if task.id in self._priority:
            return self._priority[task.id]
        best: Effective | None = None
        if task.priority is not None:
            best = Effective(task.priority)
        parent = self._open_parent(task)
        if parent is not None:
            inherited = self.effective_priority(parent)
            if inherited is not None and (best is None or inherited.value > best.value):
                best = Effective(inherited.value, parent.id, PARENT)
        for target, explicit in self.effectively_blocks(task):
            inherited = self.effective_priority(self.task(target))
            if inherited is not None and (best is None or inherited.value > best.value):
                best = Effective(inherited.value, explicit, BLOCKS)
        self._priority[task.id] = best
        return best

    def priority_for_order(self, task: Task) -> Priority:
        found = self.effective_priority(task)
        return Priority.NORMAL if found is None else found.value

    def remaining_minutes(self, task: Task) -> int:
        """Open children's remaining work, or the task's own estimate (0 if unset)."""
        if task.id in self._remaining:
            return self._remaining[task.id]
        children = self.open_children.get(task.id)
        if children:
            total = sum(self.remaining_minutes(self.task(c)) for c in children)
        else:
            total = task.estimate_minutes or 0
        self._remaining[task.id] = total
        return total

    def effective_due(self, task: Task, *, hard: bool = False) -> Effective | None:
        """The earliest of own, the parent's, and each effectively blocked task's
        deadline minus its remaining work. `hard=True` is the hard-only pass.

        Ties prefer own, then parent, then blocked tasks in id order.
        """
        own: Effective | None = None
        if task.deadline is not None and (not hard or task.deadline_firmness is Firmness.HARD):
            own = Effective(task.deadline, firmness=task.deadline_firmness)
        if task.status is not Status.OPEN:
            return own
        key = (task.id, hard)
        if key in self._due:
            return self._due[key]
        best = own
        parent = self._open_parent(task)
        if parent is not None:
            inherited = self.effective_due(parent, hard=hard)
            if inherited is not None and (best is None or inherited.value < best.value):
                best = Effective(inherited.value, parent.id, PARENT, inherited.firmness)
        for target_id, explicit in self.effectively_blocks(task):
            target = self.task(target_id)
            inherited = self.effective_due(target, hard=hard)
            if inherited is None:
                continue
            start_by = inherited.value - timedelta(minutes=self.remaining_minutes(target))
            if best is None or start_by < best.value:
                best = Effective(start_by, explicit, BLOCKS, inherited.firmness)
        self._due[key] = best
        return best

    def effective_motive(self, task: Task) -> Effective | None:
        """Own motive, or the nearest ancestor's. Not passed through blocker links."""
        if task.motive is not None or task.status is not Status.OPEN:
            return None if task.motive is None else Effective(task.motive)
        for ancestor in self.ancestors(task):
            if ancestor.motive is not None:
                return Effective(ancestor.motive, task.parent_id, PARENT)
        return None

    def is_want(self, task: Task) -> bool:
        found = self.effective_motive(task)
        return found is not None and found.value is Motive.WANT

    # --- Overdue and at risk (SPEC 5.5) ---

    def deadline_owner(self, task: Task, *, hard: bool = False) -> Task:
        """The highest of the task and its ancestors with the same effective
        deadline in this pass: whoever's work has to fit before it."""
        due = self.effective_due(task, hard=hard)
        owner = task
        while due is not None:
            parent = self._open_parent(owner)
            if parent is None:
                break
            above = self.effective_due(parent, hard=hard)
            if above is None or above.value != due.value:
                break
            owner = parent
        return owner

    def overdue(self, task: Task, *, hard: bool = False) -> bool:
        due = self.effective_due(task, hard=hard)
        return due is not None and due.value < self.now

    def at_risk(self, task: Task, *, hard: bool = False) -> bool:
        due = self.effective_due(task, hard=hard)
        if due is None or due.value < self.now:
            return False
        work = self.remaining_minutes(self.deadline_owner(task, hard=hard))
        window = max(
            timedelta(hours=AT_RISK_MIN_HOURS), timedelta(minutes=AT_RISK_WORK_FACTOR * work)
        )
        return due.value - self.now <= window

    # --- Row flags ---

    def state(self, task: Task) -> list[str]:
        """The `state` flags of SPEC 4.3, for an open task. Closed tasks have none."""
        if task.status is not Status.OPEN:
            return []
        flags: list[str] = []
        if not task.triaged:
            flags.append("inbox")
        if self.dormant(task):
            flags.append("dormant")
        if self.blocked(task):
            flags.append("blocked")
        if self.has_open_children(task):
            flags.append("has_open_children")
        if self.overdue(task):
            flags.append("overdue")
        elif self.at_risk(task):
            flags.append("at_risk")
        return flags
