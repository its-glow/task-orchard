"""The single way an Event row is written.

Call record() inside the same transaction as the change it describes, so the
log can never disagree with the data. Which events each operation writes, and
the shape of each payload, is fixed in SPEC section 6.4.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from task_orchard.core.enums import EventType
from task_orchard.db.models import Event


def record(
    session: Session,
    type: EventType,
    *,
    at: datetime,
    task_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    """Append one event. `payload` must be JSON-serialisable."""
    event = Event(at=at, type=type, task_id=task_id, payload=payload or {})
    session.add(event)
    return event
