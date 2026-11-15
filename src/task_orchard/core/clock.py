"""The only source of "now".

Nothing in core calls datetime.now() directly. The service is given a Clock, so
every time-dependent behaviour can be tested by moving a FixedClock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant as an aware datetime in UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that only moves when told to. For tests and the demo seed."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock needs an aware datetime")
        self._at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock needs an aware datetime")
        self._at = at.astimezone(UTC)

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> datetime:
        """Move forward: clock.advance(days=3) or clock.advance(timedelta(hours=1))."""
        self._at += delta if delta is not None else timedelta(**kwargs)
        return self._at
