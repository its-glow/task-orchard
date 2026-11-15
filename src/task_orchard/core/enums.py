"""Every enumerated value in Task Orchard.

Values that the model reads or writes are always exchanged by *name* ("high",
"have_to"), never by number. Ordered scales are stored as integers so that the
database can compare them; `label` and `parse` convert at the boundary.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Self


class _LabelledInt(IntEnum):
    """An ordered scale stored as an integer and spoken as a lowercase name."""

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str | int | Self) -> Self:
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            return cls(value)
        try:
            return cls[value.strip().upper()]
        except KeyError:
            allowed = ", ".join(m.label for m in cls)
            raise ValueError(f"{value!r} is not one of: {allowed}") from None


class Priority(_LabelledInt):
    """Higher is more important, so "effective priority" is a plain max()."""

    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4


class Level(_LabelledInt):
    """Shared three-step scale for activation, drain and the user's energy."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


class Status(StrEnum):
    OPEN = "open"
    DONE = "done"
    DROPPED = "dropped"
    # A scheduled instance closed by the two-live-instances cap. Never set by hand.
    MISSED = "missed"


class Firmness(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class Achievable(StrEnum):
    UNKNOWN = "unknown"
    YES = "yes"
    NO = "no"


class Motive(StrEnum):
    WANT = "want"
    SHOULD = "should"
    HAVE_TO = "have_to"


class RecurMode(StrEnum):
    SCHEDULED = "scheduled"
    FLEXIBLE = "flexible"


class FieldSource(StrEnum):
    """Where a descriptive field's value came from (SPEC section 4.3)."""

    GUESS = "guess"
    PATTERN = "pattern"
    EVIDENCE = "evidence"
    USER = "user"


# The descriptive fields whose provenance is tracked in Task.field_sources.
# `deadline` covers `deadline_firmness` as well.
TRACKED_FIELDS: tuple[str, ...] = (
    "priority",
    "deadline",
    "estimate_minutes",
    "activation",
    "drain",
    "motive",
    "conditions",
    "achievable",
)


class EventType(StrEnum):
    # Changes made through a service call.
    CREATED = "created"
    UPDATED = "updated"
    STARTED = "started"
    SNOOZED = "snoozed"
    WOKEN = "woken"
    COMPLETED = "completed"
    DROPPED = "dropped"
    REOPENED = "reopened"
    MERGED = "merged"
    SUBDIVIDED = "subdivided"
    LINKED = "linked"
    UNLINKED = "unlinked"
    PAUSED = "paused"
    RESUMED = "resumed"
    CONFLICT = "conflict"
    PATTERN_SAVED = "pattern_saved"
    PATTERNS_MERGED = "patterns_merged"
    PROFILE_SAVED = "profile_saved"
    # Changes made by roll_forward, with no caller behind them.
    SPAWNED = "spawned"
    MISSED = "missed"


# Events written by the clock, not by the user or agent. They do not count as
# "touching" a task (SPEC section 5.6).
SYSTEM_EVENTS: frozenset[EventType] = frozenset({EventType.SPAWNED, EventType.MISSED})


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    CYCLE = "cycle"
    NOT_OPEN = "not_open"
    ALREADY_OPEN = "already_open"
    HAS_OPEN_CHILDREN = "has_open_children"
    PARENT_CLOSED = "parent_closed"
    INSTANCE_EXISTS = "instance_exists"
    INSTANCE_CAP = "instance_cap"
    UNSUPPORTED = "unsupported"
    PROFILE_FULL = "profile_full"
