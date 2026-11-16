"""The schema. This file is the source of truth for what is stored.

Rules that hold for every table:

- Only stored facts live here. Blocked, dormant, candidate, effective priority,
  effective deadline, effective motive and all statistics are derived on read
  (SPEC section 5) and must never be added as columns.
- Rows are never hard-deleted, with two exceptions: a Completion is removed
  when its task is reopened, and a Pattern is removed when merged into another.
- Every instant is an aware UTC datetime (UTCDateTime). The one exception is
  Task.recur_dtstart, which is deliberately local wall-clock time.
- Any change to this file needs a new Alembic revision. tests/test_schema.py
  fails if the models and the migrations disagree.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from task_orchard.core.enums import (
    Achievable,
    EventType,
    FieldSource,
    Firmness,
    Level,
    Motive,
    Priority,
    RecurMode,
    Status,
)
from task_orchard.db.types import IntEnumType, UTCDateTime, str_enum

# Stable constraint names, so migrations can refer to them.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

LEARNED_MAX_CHARS = 600


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


class Pattern(Base):
    """A named kind of work: the merged archive the model reads instead of rows.

    Statistics are computed on read from the tasks, completions and events that
    point here (SPEC section 6.1). Only the name and the agent's short `learned`
    note are stored.
    """

    __tablename__ = "patterns"
    __table_args__ = (
        sa.CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint(
            f"learned IS NULL OR length(learned) <= {LEARNED_MAX_CHARS}", name="learned_length"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(200, collation="NOCASE"), unique=True)
    description: Mapped[str | None] = mapped_column(sa.Text)
    learned: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)

    tasks: Mapped[list[Task]] = relationship(back_populates="pattern")


class Task(Base):
    """One node in a task tree.

    Three shapes share this table:

    - a plain task, which may have children;
    - a recurring task: any task with `recur_mode` set. It is never completed,
      and its children are its instances;
    - an instance: a plain task whose parent is a recurring task. Scheduled
      instances carry `occurrence_at`, the slot they stand for.

    There is no separate "kind" column: being recurring IS having a mode.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        sa.CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        sa.CheckConstraint("parent_id IS NULL OR parent_id != id", name="not_own_parent"),
        sa.CheckConstraint("priority IS NULL OR priority BETWEEN 1 AND 4", name="priority_range"),
        sa.CheckConstraint(
            "activation IS NULL OR activation BETWEEN 1 AND 3", name="activation_range"
        ),
        sa.CheckConstraint("drain IS NULL OR drain BETWEEN 1 AND 3", name="drain_range"),
        sa.CheckConstraint(
            "estimate_minutes IS NULL OR estimate_minutes > 0", name="estimate_positive"
        ),
        # A deadline always says how firm it is, and firmness never stands alone.
        sa.CheckConstraint(
            "(deadline IS NULL) = (deadline_firmness IS NULL)", name="deadline_has_firmness"
        ),
        sa.CheckConstraint("(status = 'open') = (closed_at IS NULL)", name="closed_at_matches"),
        sa.CheckConstraint(
            "status != 'missed' OR occurrence_at IS NOT NULL", name="missed_only_instances"
        ),
        sa.CheckConstraint(
            "occurrence_at IS NULL OR parent_id IS NOT NULL", name="occurrence_has_parent"
        ),
        # Recurrence. coalesce() matters: a bare comparison with NULL yields NULL,
        # and a CHECK that evaluates to NULL passes.
        sa.CheckConstraint(
            "recur_mode IS NULL OR "
            "(deadline IS NULL AND occurrence_at IS NULL AND status IN ('open', 'dropped'))",
            name="recurring_shape",
        ),
        sa.CheckConstraint(
            "coalesce(recur_mode, '') != 'scheduled' OR "
            "(rrule IS NOT NULL AND recur_dtstart IS NOT NULL AND lead_minutes IS NOT NULL "
            "AND recur_firmness IS NOT NULL AND last_spawned_slot IS NOT NULL)",
            name="scheduled_complete",
        ),
        sa.CheckConstraint(
            "coalesce(recur_mode, '') = 'scheduled' OR "
            "(rrule IS NULL AND recur_dtstart IS NULL AND lead_minutes IS NULL "
            "AND recur_firmness IS NULL AND last_spawned_slot IS NULL)",
            name="schedule_fields_only_scheduled",
        ),
        sa.CheckConstraint(
            "coalesce(recur_mode, '') = 'flexible' OR gap_days IS NULL",
            name="gap_only_flexible",
        ),
        sa.CheckConstraint(
            "recur_mode IS NOT NULL OR cadence IS NULL", name="cadence_only_recurring"
        ),
        sa.CheckConstraint("gap_days IS NULL OR gap_days > 0", name="gap_positive"),
        sa.CheckConstraint("lead_minutes IS NULL OR lead_minutes >= 0", name="lead_not_negative"),
        # One instance per slot. This is what makes roll_forward safe to repeat.
        sa.Index(
            "uq_tasks_parent_occurrence",
            "parent_id",
            "occurrence_at",
            unique=True,
            sqlite_where=sa.text("occurrence_at IS NOT NULL"),
        ),
        sa.Index("ix_tasks_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(500))
    description: Mapped[str | None] = mapped_column(sa.Text)
    notes: Mapped[str | None] = mapped_column(sa.Text)

    status: Mapped[Status] = mapped_column(str_enum(Status, "status"), default=Status.OPEN)
    parent_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("tasks.id", ondelete="RESTRICT"), index=True
    )

    # The task's OWN values. Effective values are derived (SPEC section 5.4).
    priority: Mapped[Priority | None] = mapped_column(IntEnumType(Priority))
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deadline_firmness: Mapped[Firmness | None] = mapped_column(
        str_enum(Firmness, "deadline_firmness")
    )
    motive: Mapped[Motive | None] = mapped_column(str_enum(Motive, "motive"))

    # Not surfaced before this instant. Hides the whole subtree; on a recurring
    # task it is a pause.
    snooze_until: Mapped[datetime | None] = mapped_column(UTCDateTime)

    achievable: Mapped[Achievable] = mapped_column(
        str_enum(Achievable, "achievable"), default=Achievable.UNKNOWN
    )
    estimate_minutes: Mapped[int | None]
    activation: Mapped[Level | None] = mapped_column(IntEnumType(Level))
    drain: Mapped[Level | None] = mapped_column(IntEnumType(Level))
    # Free-form tags the task needs: ["good-weather", "at-home"]. Lowercase, hyphenated.
    conditions: Mapped[list[str]] = mapped_column(MutableList.as_mutable(sa.JSON), default=list)

    pattern_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("patterns.id", ondelete="SET NULL"), index=True
    )
    # False while the task sits in the inbox.
    triaged: Mapped[bool] = mapped_column(
        sa.Boolean(create_constraint=True, name="triaged_bool"), default=False
    )
    # {field name: FieldSource value} for the fields in enums.TRACKED_FIELDS.
    field_sources: Mapped[dict[str, str]] = mapped_column(
        MutableDict.as_mutable(sa.JSON), default=dict
    )

    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # When the task stopped being open (done, dropped or missed).
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # --- Recurring tasks only (SPEC section 7) ---
    # Set exactly when the task is recurring. Null for a plain task.
    recur_mode: Mapped[RecurMode | None] = mapped_column(str_enum(RecurMode, "recur_mode"))
    # Free text read by the agent: "about every five days", "twice a week".
    cadence: Mapped[str | None] = mapped_column(sa.Text)
    # Scheduled: an RFC 5545 RRULE string, evaluated in local wall-clock time.
    rrule: Mapped[str | None] = mapped_column(sa.String(500))
    # Scheduled: the rule's DTSTART as NAIVE LOCAL wall-clock time. Its time of
    # day is the due time of every slot. Deliberately not UTC, so that "Friday
    # 17:00" stays at 17:00 across daylight-saving changes.
    recur_dtstart: Mapped[datetime | None] = mapped_column(sa.DateTime)
    # Scheduled: how long before its slot an instance is spawned and becomes live.
    lead_minutes: Mapped[int | None]
    # Scheduled: the firmness given to each instance's deadline.
    recur_firmness: Mapped[Firmness | None] = mapped_column(str_enum(Firmness, "recur_firmness"))
    # Scheduled: the latest slot already handled. Only ever moves forward.
    last_spawned_slot: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Flexible: fallback snooze applied after each completion.
    gap_days: Mapped[int | None]

    # --- Scheduled instances only ---
    occurrence_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    parent: Mapped[Task | None] = relationship(back_populates="children", remote_side=[id])
    children: Mapped[list[Task]] = relationship(back_populates="parent", order_by="Task.id")
    pattern: Mapped[Pattern | None] = relationship(back_populates="tasks")
    completion: Mapped[Completion | None] = relationship(back_populates="task", uselist=False)

    @property
    def is_recurring(self) -> bool:
        return self.recur_mode is not None

    def __repr__(self) -> str:
        return f"<Task #{self.id} {self.status.value} {self.title!r}>"


class TaskBlock(Base):
    """`blocker` must be finished before `blocked` can start.

    Links are kept when either task closes; only an open blocker blocks.
    """

    __tablename__ = "task_blocks"
    __table_args__ = (sa.CheckConstraint("blocker_id != blocked_id", name="not_self"),)

    blocker_id: Mapped[int] = mapped_column(
        sa.ForeignKey("tasks.id", ondelete="RESTRICT"), primary_key=True
    )
    blocked_id: Mapped[int] = mapped_column(
        sa.ForeignKey("tasks.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Completion(Base):
    """What happened when a task was done. At most one per task.

    The *_at_completion columns are snapshots: history must not change when the
    tree is reshaped later.
    """

    __tablename__ = "completions"
    __table_args__ = (
        sa.CheckConstraint(
            "actual_minutes IS NULL OR actual_minutes >= 0", name="actual_not_negative"
        ),
        sa.CheckConstraint(
            "felt_activation IS NULL OR felt_activation BETWEEN 1 AND 3",
            name="felt_activation_range",
        ),
        sa.CheckConstraint(
            "felt_drain IS NULL OR felt_drain BETWEEN 1 AND 3", name="felt_drain_range"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        sa.ForeignKey("tasks.id", ondelete="RESTRICT"), unique=True
    )
    # When the work was done, which may be earlier than when it was logged.
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    actual_minutes: Mapped[int | None]
    felt_activation: Mapped[Level | None] = mapped_column(IntEnumType(Level))
    felt_drain: Mapped[Level | None] = mapped_column(IntEnumType(Level))
    reflection: Mapped[str | None] = mapped_column(sa.Text)
    estimate_at_completion: Mapped[int | None]
    # Where that estimate came from. Pattern ratios use only `guess` and `user`
    # estimates: `pattern` and `evidence` ones were derived from past completions
    # and would feed the ratio back into itself (SPEC section 6.1).
    estimate_source_at_completion: Mapped[FieldSource | None] = mapped_column(
        str_enum(FieldSource, "estimate_source_at_completion")
    )
    # The EFFECTIVE motive at the time, so the balance survives re-parenting.
    motive_at_completion: Mapped[Motive | None] = mapped_column(
        str_enum(Motive, "motive_at_completion")
    )

    task: Mapped[Task] = relationship(back_populates="completion")


class Event(Base):
    """Append-only log of every change. Never updated, never deleted.

    Written only through core.events.record(), in the same transaction as the
    change it describes. Payload shapes are listed in SPEC section 6.4.
    """

    __tablename__ = "events"
    __table_args__ = (
        sa.Index("ix_events_task_id_at", "task_id", "at"),
        sa.Index("ix_events_type_at", "type", "at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    type: Mapped[EventType] = mapped_column(str_enum(EventType, "type", constraint=False))
    # Null for events that are not about one task (patterns, profile).
    task_id: Mapped[int | None] = mapped_column(sa.ForeignKey("tasks.id", ondelete="RESTRICT"))
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)


class ProfileNote(Base):
    """What is true of the user in general. One note per topic.

    The note with topic "charter" is the user's own statement of what Task
    Orchard is for; it is returned first and has its own size limit.
    """

    __tablename__ = "profile_notes"
    __table_args__ = (
        sa.CheckConstraint("length(trim(topic)) > 0", name="topic_not_blank"),
        sa.CheckConstraint("length(trim(text)) > 0", name="text_not_blank"),
    )

    CHARTER_TOPIC = "charter"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(sa.String(100, collation="NOCASE"), unique=True)
    text: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Setting(Base):
    """Settings that live with the data rather than the environment. One row.

    `timezone` is the IANA name confirmed with the user (SPEC 3.1, 8.19), so
    clients started with different environments agree on what "today" is.
    """

    __tablename__ = "settings"
    __table_args__ = (sa.CheckConstraint("id = 1", name="one_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    timezone: Mapped[str | None] = mapped_column(sa.String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
