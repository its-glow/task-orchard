"""The service layer: every operation Task Orchard offers.

Both adapters (MCP now, HTTP later) call this class and nothing else. It is the
only code that opens transactions, and the only code that mutates rows.

How an operation is written:

    def start_task(self, task_id: int) -> dict:
        with self.tx() as tx:
            task = load_task(tx.session, task_id)      # raises NotFound
            ...validate, raising OrchardError subclasses...
            ...mutate rows...
            record(tx.session, EventType.STARTED, at=tx.now, task_id=task.id)
            return views.task_detail(tx, task)         # plain dict, built in-transaction

Rules:

- One call is one transaction. An exception rolls everything back, including
  the events.
- `tx.now` is the only "now". Never call datetime.now().
- roll_forward runs first on every call, read or write, so derived state is
  always computed against a database the clock has caught up with.
- Return plain JSON-ready dicts, built by the view functions (SPEC section 4).
  Never return ORM objects.

The operations to implement are listed in SPEC section 8, with their phase.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from task_orchard.config import Settings
from task_orchard.core import recurrence
from task_orchard.core.clock import Clock, SystemClock
from task_orchard.db.engine import init_db, make_engine, make_session_factory


@dataclass(frozen=True)
class Tx:
    """What an operation needs while it runs."""

    session: Session
    now: datetime  # aware UTC, fixed for the whole operation
    tz: ZoneInfo  # the user's timezone


class Orchard:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        tz: ZoneInfo,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self.tz = tz

    @classmethod
    def open(cls, settings: Settings, clock: Clock | None = None) -> Orchard:
        """Open (and if needed create and migrate) the database in `settings`."""
        engine = make_engine(settings.db_path)
        init_db(engine)
        return cls(make_session_factory(engine), clock or SystemClock(), settings.timezone)

    @property
    def engine(self) -> sa.Engine:
        return self._session_factory.kw["bind"]

    @contextmanager
    def tx(self) -> Iterator[Tx]:
        """One operation, one transaction, with the clock caught up first."""
        now = self._clock.now()
        with self._session_factory() as session, session.begin():
            recurrence.roll_forward(session, now, self.tz)
            yield Tx(session=session, now=now, tz=self.tz)

    # Operations go here, phase by phase (SPEC section 8).
