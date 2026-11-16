"""Small helpers shared by the service tests."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from task_orchard.core.service import Orchard
from task_orchard.db.models import Completion, Event, Task


def make(orchard: Orchard, title: str = "Task", **kw: Any) -> int:
    return orchard.create_task(title=title, **kw)["task"]["id"]


def events(orchard: Orchard, since: int = 0) -> list[tuple[str, int | None, dict]]:
    """(type, task id, payload) for every event after `since`, in order."""
    with orchard.tx() as tx:
        rows = tx.session.scalars(sa.select(Event).where(Event.id > since).order_by(Event.id))
        return [(e.type.value, e.task_id, e.payload) for e in rows]


def last_event_id(orchard: Orchard) -> int:
    with orchard.tx() as tx:
        return tx.session.scalar(sa.select(sa.func.max(Event.id))) or 0


def counts(orchard: Orchard) -> tuple[int, int, int]:
    """Tasks, events and completions: a failed call must change none of them."""
    with orchard.tx() as tx:
        s = tx.session
        return tuple(
            s.scalar(sa.select(sa.func.count()).select_from(model))
            for model in (Task, Event, Completion)
        )


def completion(orchard: Orchard, task_id: int) -> Completion | None:
    with orchard.tx() as tx:
        found = tx.session.scalar(sa.select(Completion).where(Completion.task_id == task_id))
        if found is not None:
            tx.session.expunge(found)
        return found
