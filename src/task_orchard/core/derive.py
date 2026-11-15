"""Derived state, computed on read and never stored.

PHASE 1: open, dormant, blocked, has_open_children, is_instance, candidate,
and the row `state` flags.
PHASE 2: effective priority, effective deadline (any and hard), effective
motive, remaining minutes, at-risk, each with where it came from.

A Snapshot loads every open task and every blocker link once per request, and
answers each question from that (SPEC section 5). Tasks outside the open set
(a closed ancestor, a closed task being shown) are fetched on demand. Nothing
is cached between requests.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from task_orchard.core.enums import RecurMode, Status
from task_orchard.db.models import Task, TaskBlock


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

    def is_open(self, task_id: int) -> bool:
        return task_id in self.open_ids

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

    # --- Row flags ---

    def overdue(self, task: Task) -> bool:
        # Phase 1 judges the task's own deadline; Phase 2 moves this to `due`.
        return task.deadline is not None and task.deadline < self.now

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
        return flags
