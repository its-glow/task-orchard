"""The service layer: every operation Task Orchard offers.

Both adapters (MCP now, HTTP later) call this class and nothing else. It is the
only code that opens transactions, and the only code that mutates rows.

How an operation is written:

    def start_task(self, task_id: int) -> dict:
        with self.tx() as tx:
            task = self._task(tx, task_id)              # raises NotFound
            ...validate, raising OrchardError subclasses...
            ...mutate rows...
            record(tx.session, EventType.STARTED, at=tx.now, task_id=task.id)
            return self._respond(tx, {"task": ...})     # plain dict, built in-transaction

Rules:

- One call is one transaction. An exception rolls everything back, including
  the events.
- `tx.now` is the only "now". Never call datetime.now().
- roll_forward runs first on every call, read or write, so derived state is
  always computed against a database the clock has caught up with.
- Return plain JSON-ready dicts, built by the view functions (SPEC section 4).
  Never return ORM objects.

The operations are listed in SPEC section 8, with their phase.
"""

from __future__ import annotations

import functools
import json
import re
import zoneinfo
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from task_orchard.config import Settings
from task_orchard.core import graph, history, review, shortlist, views, wire
from task_orchard.core import recurrence as recur
from task_orchard.core.clock import Clock, SystemClock
from task_orchard.core.derive import Snapshot
from task_orchard.core.enums import (
    TRACKED_FIELDS,
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
from task_orchard.core.errors import (
    AlreadyOpen,
    CycleError,
    HasOpenChildren,
    InstanceCap,
    InstanceExists,
    Invalid,
    NotFound,
    NotOpen,
    OrchardError,
    ParentClosed,
    ProfileFull,
    Unsupported,
)
from task_orchard.core.events import record
from task_orchard.core.timefmt import (
    DayMeans,
    at_text,
    day_snooze_end,
    parse_when,
    relative,
    stamp,
)
from task_orchard.db.engine import init_db, make_engine, make_session_factory
from task_orchard.db.models import (
    LEARNED_MAX_CHARS,
    Completion,
    Pattern,
    ProfileNote,
    Setting,
    Task,
    TaskBlock,
)

SKIP_HINT = (
    "Some fields were not changed because the user set them. If the user asked for this "
    "change, repeat the call with sources={sources}."
)
CHILDREN_HINT = (
    "A task with children is not one step. Add a child for the remaining work, or complete "
    "this task."
)
RECENT_COMPLETIONS = 5
RECURRENCE_NOTE = (
    "Adjust when this comes back with update_task(snooze_until=...), or create an instance "
    "under it with its own deadline."
)

# Fields update_task may change on a closed task (SPEC 8.3).
CLOSED_EDITABLE = frozenset({"description", "notes", "append_note", "pattern_id"})
CLEARABLE = (
    "description",
    "notes",
    "priority",
    "deadline",
    "snooze_until",
    "estimate_minutes",
    "activation",
    "drain",
    "motive",
    "conditions",
    "pattern_id",
    "parent_id",
    "cadence",
    "gap_days",
)
SEARCH_MAX = 30
CANDIDATES_MAX = 30
DUE_WITHIN_MAX_DAYS = 365
HORIZON_MAX_DAYS = 30
RESERVE_WANTS_MAX = 10
CHARTER_MAX_CHARS = 4_000
NOTE_MAX_CHARS = 1_000
NOTES_TOTAL_MAX_CHARS = 8_000
TOPIC_MAX_CHARS = 100
SEARCH_MAX_WORDS = 20
TREE_MAX_NODES = 100
DUPLICATES_PER_ITEM = 3
SUBDIVIDE_MAX_CHILDREN = 12
DROP_MAX_TASKS = 50
UNBLOCKED_MAX = 10
PARENT_CHECK_CHILDREN = 10
PATTERNS_MAX = 10
PATTERN_NAME_MAX_CHARS = 200
# What a child in subdivide_task may carry (SPEC 8.4).
CHILD_KEYS = frozenset(
    {
        "title",
        "description",
        "estimate_minutes",
        "achievable",
        "priority",
        "activation",
        "drain",
        "conditions",
        "after",
    }
)


@dataclass(frozen=True)
class Tx:
    """What an operation needs while it runs."""

    session: Session
    now: datetime  # aware UTC, fixed for the whole operation
    tz: ZoneInfo  # the user's timezone


@dataclass
class _Write:
    """What a create or update is about to change, and what it has changed."""

    changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


class Orchard:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        tz: ZoneInfo,
        *,
        tz_fixed: bool = False,
    ) -> None:
        """`tz` is the zone from the environment. Unless `tz_fixed` (TASK_ORCHARD_TZ
        was set), a zone stored by set_timezone takes its place (SPEC 3.1)."""
        self._session_factory = session_factory
        self._clock = clock
        self.tz = tz
        self.tz_fixed = tz_fixed

    @classmethod
    def open(cls, settings: Settings, clock: Clock | None = None) -> Orchard:
        """Open (and if needed create and migrate) the database in `settings`."""
        engine = make_engine(settings.db_path)
        init_db(engine)
        return cls(
            make_session_factory(engine),
            clock or SystemClock(),
            settings.timezone,
            tz_fixed=settings.timezone_explicit,
        )

    @property
    def engine(self) -> sa.Engine:
        return self._session_factory.kw["bind"]

    @contextmanager
    def tx(self) -> Iterator[Tx]:
        """One operation, one transaction, with the clock caught up first."""
        now = self._clock.now()
        with self._session_factory() as session, session.begin():
            tz = self._zone(session)
            recur.roll_forward(session, now, tz)
            yield Tx(session=session, now=now, tz=tz)

    def _zone(self, session: Session) -> ZoneInfo:
        if self.tz_fixed:
            return self.tz
        setting = session.get(Setting, 1)
        if setting is None or setting.timezone is None:
            return self.tz
        return ZoneInfo(setting.timezone)

    # --- set_timezone (8.19) ---

    def set_timezone(self, name: str) -> dict[str, Any]:
        zone = _zone_named(name)
        with self.tx() as tx:
            s = tx.session
            setting = s.get(Setting, 1)
            if setting is None:
                setting = Setting(id=1, updated_at=tx.now)
                s.add(setting)
            if setting.timezone != zone:
                record(
                    s,
                    EventType.SETTINGS_SAVED,
                    at=tx.now,
                    payload={"timezone": {"from": setting.timezone, "to": zone}},
                )
                setting.timezone = zone
                setting.updated_at = tx.now
            note = None
            if self.tz_fixed and self.tz.key != zone:
                note = (
                    f"Stored, but TASK_ORCHARD_TZ is set to {self.tz.key} in this client's "
                    "environment and overrides it. Tell the user to remove or change it."
                )
            shown = Tx(session=s, now=tx.now, tz=tx.tz if note else ZoneInfo(zone))
            return self._respond(shown, {"timezone": zone, "note": note})

    def stored_timezone(self) -> str | None:
        """The zone set_timezone stored, if any. For start-up checks, not for the model."""
        with self._session_factory() as session:
            setting = session.get(Setting, 1)
            return None if setting is None else setting.timezone

    # --- Helpers ---

    @staticmethod
    def _task(tx: Tx, task_id: Any, what: str = "task") -> Task:
        if isinstance(task_id, bool) or not isinstance(task_id, int):
            raise Invalid(f"{what} id must be a number.", hint="Ids come from tool results.")
        task = tx.session.get(Task, task_id)
        if task is None:
            raise NotFound(what, task_id)
        return task

    @staticmethod
    def _respond(tx: Tx, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return views.respond(tx.now, tx.tz, payload, **kw)

    def _detail(self, tx: Tx, task: Task) -> dict[str, Any]:
        snap = Snapshot(tx.session, tx.now)
        return views.task_detail(snap, task, tx.tz)

    @staticmethod
    def _structure(tx: Tx) -> graph.Structure:
        s = tx.session
        return graph.Structure(
            parent=dict(s.execute(sa.select(Task.id, Task.parent_id)).all()),
            blocks=set(s.execute(sa.select(TaskBlock.blocker_id, TaskBlock.blocked_id)).all()),
        )

    def _check_cycle(
        self,
        tx: Tx,
        *,
        add_blocks: list[tuple[int, int]] = (),  # type: ignore[assignment]
        set_parent: dict[int, int | None] | None = None,
    ) -> None:
        steps = graph.check(self._structure(tx), add_blocks=add_blocks, set_parent=set_parent)
        if steps is not None:
            self._raise_cycle(tx, steps)

    @staticmethod
    def _raise_cycle(tx: Tx, steps: list[tuple[int, str | None]]) -> None:
        path = []
        for task_id, because in steps:
            task = tx.session.get(Task, task_id)
            step = {
                "id": task_id,
                "title": views.truncate(task.title, views.NESTED_TITLE_CHARS),
                "status": task.status.value,
            }
            if because is not None:
                step["because"] = because
            path.append(step)
        raise CycleError(path)

    @staticmethod
    def _open_parent(tx: Tx, parent: Task) -> None:
        if parent.status is not Status.OPEN:
            raise ParentClosed(parent.id, parent.status.value)
        if parent.is_recurring:
            raise Unsupported(
                f"Task {parent.id} is recurring; tasks cannot be put under it this way.",
                hint=(
                    "Instances of recurring tasks are created by the server. Put this task "
                    "under an ordinary task, or leave it at the top level."
                ),
                parent_id=parent.id,
            )

    @staticmethod
    def _flip_parent_achievable(tx: Tx, parent: Task) -> None:
        """Adding a child makes the parent a container (SPEC 8.2). Structural:
        it overrides even a user-set value, and the source becomes `evidence`."""
        if parent.is_recurring or parent.achievable is Achievable.NO:
            return
        before = parent.achievable.value
        parent.achievable = Achievable.NO
        parent.field_sources["achievable"] = FieldSource.EVIDENCE.value
        parent.updated_at = tx.now
        record(
            tx.session,
            EventType.UPDATED,
            at=tx.now,
            task_id=parent.id,
            payload={
                "changes": {
                    "achievable": {
                        "from": before,
                        "to": Achievable.NO.value,
                        "source": FieldSource.EVIDENCE.value,
                    }
                }
            },
        )

    @staticmethod
    def _new_task(
        tx: Tx,
        values: dict[str, Any],
        src: dict[str, FieldSource],
        *,
        parent_id: int | None,
    ) -> Task:
        """A new triaged task from parsed field values. Tracked fields take
        their source from `src`, `guess` if none (SPEC 4.5)."""
        task = Task(
            title=values["title"],
            description=values.get("description"),
            notes=values.get("notes"),
            status=Status.OPEN,
            parent_id=parent_id,
            triaged=True,
            achievable=Achievable.UNKNOWN,
            conditions=[],
            field_sources={},
            pattern_id=values.get("pattern_id"),
            snooze_until=values.get("snooze_until"),
            created_at=tx.now,
            updated_at=tx.now,
        )
        for name in ("priority", "estimate_minutes", "activation", "drain", "motive"):
            if name in values:
                setattr(task, name, values[name])
                task.field_sources[name] = src.get(name, FieldSource.GUESS).value
        if values.get("conditions"):
            task.conditions = values["conditions"]
            task.field_sources["conditions"] = src.get("conditions", FieldSource.GUESS).value
        if "achievable" in values:
            task.achievable = values["achievable"]
            task.field_sources["achievable"] = src.get("achievable", FieldSource.GUESS).value
        if "deadline" in values:
            task.deadline = values["deadline"]
            task.deadline_firmness = values.get("firmness", Firmness.SOFT)
            task.field_sources["deadline"] = src.get("deadline", FieldSource.GUESS).value
        tx.session.add(task)
        tx.session.flush()
        return task

    @staticmethod
    def _close(tx: Tx, task: Task, status: Status) -> None:
        task.status = status
        task.closed_at = tx.now
        task.updated_at = tx.now

    def _drop_descendants(
        self, tx: Tx, before: Snapshot, task: Task, reason: str, skip: frozenset[int] = frozenset()
    ) -> list[Task]:
        """Drop every open descendant of `task` (as of `before`), each with a
        `dropped` event naming `task` as the cascade's origin."""
        out: list[Task] = []
        for i in before.open_descendants(task.id):
            child = before.task(i)
            if i in skip or child.status is not Status.OPEN:
                continue
            self._close(tx, child, Status.DROPPED)
            record(
                tx.session,
                EventType.DROPPED,
                at=tx.now,
                task_id=i,
                payload={"reason": reason, "cascade_from": task.id},
            )
            out.append(child)
        return out

    @staticmethod
    def _unblocked(
        tx: Tx, before: Snapshot, after: Snapshot, closed: set[int]
    ) -> tuple[list[dict[str, Any]], int]:
        """Tasks for which the closed tasks were the last open effective blockers.

        Every open task that was blocked before the call and is not after it,
        among those the closed tasks effectively blocked (SPEC 8.7). Capped at
        UNBLOCKED_MAX rows, with the rest counted.
        """
        reached: set[int] = set()
        for task_id in closed:
            reached.update(t for t, _ in before.effectively_blocks(before.task(task_id)))
        free = sorted(
            t for t in reached - closed if after.is_open(t) and not after.blocked(after.task(t))
        )
        rows = [views.task_row(after, after.task(t), tx.tz) for t in free[:UNBLOCKED_MAX]]
        return rows, max(0, len(free) - UNBLOCKED_MAX)

    @staticmethod
    def _parent_check(tx: Tx, after: Snapshot, parent_id: int | None) -> dict[str, Any] | None:
        """For an open plain parent left with no open children (SPEC 8.7)."""
        if parent_id is None:
            return None
        parent = after.task(parent_id)
        if parent.status is not Status.OPEN or parent.is_recurring:
            return None
        if after.has_open_children(parent):
            return None
        children = tx.session.scalars(
            sa.select(Task)
            .where(Task.parent_id == parent.id)
            .order_by(Task.closed_at.desc(), Task.id.desc())
            .limit(PARENT_CHECK_CHILDREN)
        )
        return {
            "parent": views.task_row(after, parent, tx.tz),
            "children": [
                {"title": views.truncate(c.title, views.ROW_TITLE_CHARS), "status": c.status.value}
                for c in children
            ],
        }

    @staticmethod
    def _check_convertible(tx: Tx, task: Task, *, clearing_deadline: bool) -> None:
        """A plain task can become recurring only in the shape of SPEC 7.3."""
        problem = None
        if task.children:
            problem = "it has children"
        elif task.parent_id is not None and tx.session.get(Task, task.parent_id).is_recurring:
            problem = "it is an occurrence of a recurring task"
        elif task.deadline is not None and not clearing_deadline:
            problem = "it has a deadline"
        elif tx.session.scalar(
            sa.select(sa.func.count())
            .select_from(TaskBlock)
            .join(Task, Task.id == TaskBlock.blocked_id)
            .where(TaskBlock.blocker_id == task.id, Task.status == Status.OPEN)
        ):
            problem = "it blocks other tasks, and a recurring task cannot be a blocker"
        if problem is not None:
            raise Invalid(
                f"Task {task.id} cannot become recurring: {problem}.",
                hint="A task can become recurring only when it is open, has no children, is "
                "not an occurrence and blocks nothing. Clear a deadline in the same call with "
                'clear=["deadline"]. Otherwise create a new recurring task.',
                task_id=task.id,
            )

    @staticmethod
    def _has_live_children(task: Task) -> bool:
        """Open or done children. Dropped and missed ones do not count (SPEC 8.2)."""
        return any(c.status in (Status.OPEN, Status.DONE) for c in task.children)

    def _parse_fields(self, tx: Tx, given: dict[str, Any]) -> dict[str, Any]:
        """Parse every field parameter that was passed (not None) into its stored form."""
        out: dict[str, Any] = {}
        for name, value in given.items():
            if value is None:
                continue
            if name == "title":
                out[name] = wire.title(value)
            elif name in ("description", "notes"):
                out[name] = wire.text(value, name)
            elif name == "priority":
                out[name] = wire.parse_enum(Priority, value, name)
            elif name in ("activation", "drain"):
                out[name] = wire.parse_enum(Level, value, name)
            elif name == "motive":
                out[name] = wire.parse_enum(Motive, value, name)
            elif name == "firmness":
                out[name] = wire.parse_enum(Firmness, value, name)
            elif name == "achievable":
                out[name] = wire.parse_enum(Achievable, value, name)
            elif name == "deadline":
                out[name] = parse_when(value, tx.tz, tx.now, DayMeans.DEADLINE)
            elif name == "snooze_until":
                out[name] = parse_when(value, tx.tz, tx.now, DayMeans.SNOOZE)
            elif name == "estimate_minutes":
                out[name] = wire.positive_int(value, name)
            elif name == "conditions":
                out[name] = wire.conditions(value)
            elif name == "pattern_id":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise Invalid("pattern_id must be a number.", hint="Use find_patterns.")
                if tx.session.get(Pattern, value) is None:
                    raise NotFound("pattern", value)
                out[name] = value
            else:  # pragma: no cover - the callers pass known names only
                raise AssertionError(name)
        if "notes" in out:
            wire.check_notes_length(out["notes"])
        return out

    # --- Tracked fields and their protection (SPEC 4.5) ---

    @staticmethod
    def _apply_tracked(
        tx: Tx,
        task: Task,
        field_name: str,
        attrs: dict[str, Any],
        proposed: Any,
        incoming: FieldSource,
        write: _Write,
    ) -> None:
        """Write one tracked field, unless the user set it and this call is not the user."""

        def current_value() -> Any:
            if field_name == "achievable":
                return task.achievable.value
            return views.wire_value(field_name, task, tx.tz)

        current = task.field_sources.get(field_name)
        kept = current_value()
        if current == FieldSource.USER.value and incoming is not FieldSource.USER:
            write.skipped.append(
                {
                    "field": field_name,
                    "kept": kept,
                    "proposed": proposed,
                    "reason": "set by the user",
                }
            )
            if incoming in (FieldSource.PATTERN, FieldSource.EVIDENCE):
                write.conflicts.append(
                    {
                        "field": field_name,
                        "kept": kept,
                        "proposed": proposed,
                        "source": incoming.value,
                    }
                )
            return
        for attr, value in attrs.items():
            setattr(task, attr, value)
        after = current_value()
        cleared = after is None
        if cleared:
            task.field_sources.pop(field_name, None)
        else:
            task.field_sources[field_name] = incoming.value
        if after != kept or (not cleared and current != incoming.value):
            change: dict[str, Any] = {"from": kept, "to": after}
            if not cleared:
                change["source"] = incoming.value
            write.changes[field_name] = change

    # --- capture_tasks (8.1) ---

    def capture_tasks(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(items, list) or not 1 <= len(items) <= wire.CAPTURE_MAX_ITEMS:
            raise Invalid(
                f"items must be a list of 1 to {wire.CAPTURE_MAX_ITEMS} things to capture.",
                hint='For example items=[{"title": "Call the dentist"}].',
            )
        with self.tx() as tx:
            s = tx.session
            existing = [
                (t.id, t.title, review.normalise_title(t.title))
                for t in s.scalars(
                    sa.select(Task).where(Task.status == Status.OPEN).order_by(Task.id)
                )
            ]
            captured: list[dict[str, Any]] = []
            duplicates: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    raise Invalid("Each item must be an object.", hint='{"title": "..."}')
                title = wire.title(item.get("title"))
                notes = item.get("notes")
                if notes is not None:
                    wire.check_notes_length(wire.text(notes, "notes"))
                task = Task(
                    title=title,
                    notes=notes or None,
                    status=Status.OPEN,
                    triaged=False,
                    achievable=Achievable.UNKNOWN,
                    conditions=[],
                    field_sources={},
                    created_at=tx.now,
                    updated_at=tx.now,
                )
                s.add(task)
                s.flush()
                record(s, EventType.CREATED, at=tx.now, task_id=task.id, payload={"via": "capture"})
                key = review.normalise_title(title)
                matches = [(i, t) for i, t, k in existing if k == key][:DUPLICATES_PER_ITEM]
                for other_id, other_title in matches:
                    duplicates.append(
                        {
                            "new_id": task.id,
                            "existing": {
                                "id": other_id,
                                "title": views.truncate(other_title, views.NESTED_TITLE_CHARS),
                            },
                        }
                    )
                existing.append((task.id, title, key))
                captured.append(
                    {"id": task.id, "title": views.truncate(title, views.ROW_TITLE_CHARS)}
                )
            return self._respond(tx, {"captured": captured, "possible_duplicates": duplicates})

    # --- create_task (8.2) ---

    def create_task(
        self,
        title: str | None = None,
        *,
        description: str | None = None,
        notes: str | None = None,
        parent_id: int | None = None,
        priority: str | None = None,
        deadline: str | None = None,
        firmness: str | None = None,
        snooze_until: str | None = None,
        achievable: str | None = None,
        estimate_minutes: int | None = None,
        activation: str | None = None,
        drain: str | None = None,
        motive: str | None = None,
        conditions: list[str] | None = None,
        pattern_id: int | None = None,
        blocked_by: list[int] | None = None,
        blocks: list[int] | None = None,
        recurrence: dict[str, Any] | None = None,
        sources: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self.tx() as tx:
            s = tx.session
            src = wire.sources(sources)
            parent = None
            if parent_id is not None:
                parent = self._task(tx, parent_id, "parent task")
                if parent.status is not Status.OPEN:
                    raise ParentClosed(parent.id, parent.status.value)
            instance_of = parent if parent is not None and parent.is_recurring else None
            if instance_of is not None:
                if recurrence is not None:
                    raise Invalid(
                        "A task under a recurring task is one of its occurrences, so it cannot "
                        "be recurring itself.",
                        hint="Leave recurrence out, or create the task somewhere else.",
                    )
                if instance_of.recur_mode is RecurMode.SCHEDULED:
                    raise Unsupported(
                        f"Task {instance_of.id} is scheduled: its occurrences are created on "
                        "schedule.",
                        hint="To record one done early, call complete_task with the recurring "
                        "task's id. To change an occurrence, use update_task on it.",
                        parent_id=instance_of.id,
                    )
                live = recur.open_instances(s, instance_of)
                if live:
                    raise InstanceExists(instance_of.id, live[0].id)
                if title is None:
                    title = instance_of.title
            parsed = None
            if recurrence is not None:
                parsed = recur.parse_recurrence(recurrence, tx.tz)
                _reject_for_recurring(deadline=deadline, firmness=firmness, achievable=achievable)
            values = self._parse_fields(
                tx,
                {
                    "title": title if title is not None else "",
                    "description": description,
                    "notes": notes,
                    "priority": priority,
                    "deadline": deadline,
                    "firmness": firmness,
                    "snooze_until": snooze_until,
                    "achievable": achievable,
                    "estimate_minutes": estimate_minutes,
                    "activation": activation,
                    "drain": drain,
                    "motive": motive,
                    "conditions": conditions,
                    "pattern_id": pattern_id,
                },
            )
            if "firmness" in values and "deadline" not in values:
                raise Invalid(
                    "firmness needs a deadline.",
                    hint="Pass deadline as well, or leave firmness out.",
                )

            blocker_ids = wire.ids(blocked_by, "blocked_by")
            blocked_ids = wire.ids(blocks, "blocks")
            for bid in blocker_ids:
                blocker = self._task(tx, bid)
                if blocker.status is not Status.OPEN:
                    raise NotOpen(bid, blocker.status.value, doing="use it as a blocker")
                _reject_recurring_blocker(blocker)
            for bid in blocked_ids:
                blocked = self._task(tx, bid)
                if blocked.status is not Status.OPEN:
                    raise NotOpen(bid, blocked.status.value, doing="block it")
            if parsed is not None and blocked_ids:
                raise Unsupported(
                    "A recurring task cannot block other tasks.",
                    hint="Leave blocks out. Once an occurrence exists, link that instead.",
                )

            task = self._new_task(tx, values, src, parent_id=parent_id)
            if parsed is not None:
                recur.apply(task, parsed, tx.now, tx.tz, creating=True)
                s.flush()

            links = [(b, task.id) for b in blocker_ids] + [(task.id, b) for b in blocked_ids]
            self._check_cycle(tx, add_blocks=links, set_parent={task.id: parent_id})

            record(
                s,
                EventType.CREATED,
                at=tx.now,
                task_id=task.id,
                payload={"via": "create", "parent_id": parent_id},
            )
            for blocker_id, blocked_id in links:
                s.add(TaskBlock(blocker_id=blocker_id, blocked_id=blocked_id, created_at=tx.now))
                record(
                    s,
                    EventType.LINKED,
                    at=tx.now,
                    task_id=blocked_id,
                    payload={"blocker_id": blocker_id, "blocked_id": blocked_id},
                )
            if parent is not None:
                self._flip_parent_achievable(tx, parent)
            sleeping = instance_of is not None and instance_of.snooze_until is not None
            if sleeping and instance_of.snooze_until > tx.now:
                # Choosing the next occurrence wakes a sleeping flexible task (SPEC 7.2).
                before = at_text(instance_of.snooze_until, tx.tz)
                instance_of.snooze_until = None
                instance_of.updated_at = tx.now
                record(
                    s,
                    EventType.UPDATED,
                    at=tx.now,
                    task_id=instance_of.id,
                    payload={"changes": {"snooze_until": {"from": before, "to": None}}},
                )
            s.flush()
            s.refresh(task)
            payload: dict[str, Any] = {"task": self._detail(tx, task)}
            if task.recur_mode is RecurMode.SCHEDULED:
                payload["next_slots"] = [
                    stamp(slot, tx.tz, tx.now)
                    for slot in recur.next_slots(task, task.last_spawned_slot, tx.tz)
                ]
            return self._respond(tx, payload)

    # --- update_task (8.3) ---

    def update_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        notes: str | None = None,
        append_note: str | None = None,
        parent_id: int | None = None,
        priority: str | None = None,
        deadline: str | None = None,
        firmness: str | None = None,
        snooze_until: str | None = None,
        achievable: str | None = None,
        estimate_minutes: int | None = None,
        activation: str | None = None,
        drain: str | None = None,
        motive: str | None = None,
        conditions: list[str] | None = None,
        pattern_id: int | None = None,
        clear: list[str] | None = None,
        triaged: bool | None = None,
        recurrence: dict[str, Any] | None = None,
        sources: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self.tx() as tx:
            s = tx.session
            task = self._task(tx, task_id)
            src = wire.sources(sources)
            to_clear = wire.names(clear, CLEARABLE, "clear")
            given = {
                "title": title,
                "description": description,
                "notes": notes,
                "append_note": append_note,
                "parent_id": parent_id,
                "priority": priority,
                "deadline": deadline,
                "firmness": firmness,
                "snooze_until": snooze_until,
                "achievable": achievable,
                "estimate_minutes": estimate_minutes,
                "activation": activation,
                "drain": drain,
                "motive": motive,
                "conditions": conditions,
                "pattern_id": pattern_id,
            }
            passed = {k for k, v in given.items() if v is not None}
            if triaged is not None and not isinstance(triaged, bool):
                raise Invalid("triaged must be true or false.", hint="For example triaged=false.")

            # Validate the shape of the call before anything changes.
            both = sorted(set(to_clear) & passed)
            if "firmness" in passed and "deadline" in to_clear:
                both.append("firmness")
            if both:
                raise Invalid(
                    f"{', '.join(both)} cannot be set and cleared in the same call.",
                    hint="Either pass a new value or name the field in clear, not both.",
                )
            if "append_note" in passed and ("notes" in passed or "notes" in to_clear):
                raise Invalid(
                    "append_note cannot be combined with setting or clearing notes.",
                    hint="Use notes to replace them, or append_note to add a line, not both.",
                )
            if task.status is not Status.OPEN:
                blocked = sorted((passed | set(to_clear)) - CLOSED_EDITABLE)
                if blocked:
                    raise NotOpen(task.id, task.status.value, doing=f"change {', '.join(blocked)}")
            if recurrence is not None and task.status is not Status.OPEN:
                raise NotOpen(task.id, task.status.value, doing="change its recurrence")
            parsed = None
            if recurrence is not None:
                parsed = recur.parse_recurrence(recurrence, tx.tz, mode=task.recur_mode)
            converting = parsed is not None and not task.is_recurring
            recurring_after = task.is_recurring or converting
            for name in ("cadence", "gap_days"):
                applies = recurring_after and (
                    name == "cadence"
                    or (parsed.mode if converting else task.recur_mode) is RecurMode.FLEXIBLE
                )
                if name in to_clear and not applies:
                    raise Invalid(
                        f"Task {task.id} has no {name} to clear.",
                        hint=f"Remove {name} from clear. "
                        "cadence belongs to recurring tasks, gap_days to flexible ones.",
                    )
            if recurring_after:
                _reject_for_recurring(deadline=deadline, firmness=firmness, achievable=achievable)
            if converting:
                self._check_convertible(tx, task, clearing_deadline="deadline" in to_clear)

            values = self._parse_fields(
                tx, {k: v for k, v in given.items() if k not in ("append_note", "parent_id")}
            )
            if "append_note" in passed:
                values["append_note"] = wire.text(append_note, "append_note")

            if "firmness" in values and "deadline" not in values and task.deadline is None:
                raise Invalid(
                    f"Task {task.id} has no deadline, so its firmness cannot change.",
                    hint="Pass deadline as well, or leave firmness out.",
                )
            if values.get("achievable") is Achievable.YES and self._has_live_children(task):
                raise Invalid(
                    f"Task {task.id} has children, so it cannot be achievable.",
                    hint=CHILDREN_HINT,
                )

            new_parent: Task | None = None
            moving = False
            if "parent_id" in passed or "parent_id" in to_clear:
                target = parent_id if "parent_id" in passed else None
                if target != task.parent_id:
                    if task.parent_id is not None and s.get(Task, task.parent_id).is_recurring:
                        raise Unsupported(
                            f"Task {task.id} is an instance of a recurring task and cannot move.",
                            hint="Create a separate task where you want it instead.",
                        )
                    if target is not None:
                        new_parent = self._task(tx, target, "parent task")
                        if new_parent.id != task.id:
                            self._open_parent(tx, new_parent)
                    self._check_cycle(tx, set_parent={task.id: target})
                    moving = True

            write = _Write()
            recurrence_before = recur.describe(task, tx.tz) if task.is_recurring else None
            snooze_before = task.snooze_until
            before_untracked = {
                "title": task.title,
                "snooze_until": views.wire_value("snooze_until", task, tx.tz),
                "pattern_id": task.pattern_id,
                "parent_id": task.parent_id,
                "triaged": task.triaged,
            }
            before_text = {"description": task.description, "notes": task.notes}

            # Untracked fields: no source, never protected.
            if "title" in values:
                task.title = values["title"]
            if "description" in values:
                task.description = values["description"]
            if "description" in to_clear:
                task.description = None
            if "notes" in values:
                task.notes = values["notes"]
            if "notes" in to_clear:
                task.notes = None
            if "append_note" in values:
                task.notes = (
                    f"{task.notes}\n{values['append_note']}"
                    if task.notes
                    else values["append_note"]
                )
                wire.check_notes_length(task.notes)
            if "snooze_until" in values:
                task.snooze_until = values["snooze_until"]
            if "snooze_until" in to_clear:
                task.snooze_until = None
            if "pattern_id" in values:
                task.pattern_id = values["pattern_id"]
            if "pattern_id" in to_clear:
                task.pattern_id = None
            if moving:
                task.parent_id = new_parent.id if new_parent is not None else None
            if task.status is Status.OPEN and triaged is not False:
                task.triaged = True
            elif task.status is Status.OPEN:
                task.triaged = False

            # Tracked fields, each protected.
            for name in ("priority", "estimate_minutes", "activation", "drain", "motive"):
                if name in values:
                    proposed = views.wire_name(values[name])
                    self._apply_tracked(
                        tx, task, name, {name: values[name]}, proposed,
                        src.get(name, FieldSource.GUESS), write,
                    )  # fmt: skip
                elif name in to_clear:
                    self._apply_tracked(
                        tx, task, name, {name: None}, None, src.get(name, FieldSource.GUESS), write
                    )
            if "conditions" in values:
                self._apply_tracked(
                    tx, task, "conditions", {"conditions": values["conditions"]},
                    values["conditions"] or None, src.get("conditions", FieldSource.GUESS), write,
                )  # fmt: skip
            elif "conditions" in to_clear:
                self._apply_tracked(
                    tx, task, "conditions", {"conditions": []}, None,
                    src.get("conditions", FieldSource.GUESS), write,
                )  # fmt: skip
            if "achievable" in values:
                self._apply_tracked(
                    tx, task, "achievable", {"achievable": values["achievable"]},
                    values["achievable"].value, src.get("achievable", FieldSource.GUESS), write,
                )  # fmt: skip
            if "deadline" in values or "firmness" in values:
                when = values.get("deadline", task.deadline)
                how = values.get("firmness") or task.deadline_firmness or Firmness.SOFT
                self._apply_tracked(
                    tx, task, "deadline", {"deadline": when, "deadline_firmness": how},
                    {"at": at_text(when, tx.tz), "firmness": how.value},
                    src.get("deadline", FieldSource.GUESS), write,
                )  # fmt: skip
            elif "deadline" in to_clear:
                self._apply_tracked(
                    tx, task, "deadline", {"deadline": None, "deadline_firmness": None}, None,
                    src.get("deadline", FieldSource.GUESS), write,
                )  # fmt: skip

            if converting and task.deadline is not None:
                raise Invalid(
                    f"Task {task.id} keeps its deadline because the user set it, so it cannot "
                    "become recurring.",
                    hint='If the user asked for this, repeat with sources={"deadline": "user"}.',
                )
            for name in ("cadence", "gap_days"):
                if name in to_clear:
                    setattr(task, name, None)
            if parsed is not None:
                if converting:
                    task.achievable = Achievable.UNKNOWN
                    task.field_sources.pop("achievable", None)
                recur.apply(task, parsed, tx.now, tx.tz, creating=converting)
            if task.is_recurring:
                after = recur.describe(task, tx.tz)
                if after != recurrence_before:
                    write.changes["recurrence"] = {"from": recurrence_before, "to": after}
            pause_event = None
            if task.recur_mode is RecurMode.SCHEDULED and task.snooze_until != snooze_before:
                if task.snooze_until is not None:
                    pause_event = (
                        EventType.PAUSED,
                        {"until": at_text(task.snooze_until, tx.tz)},
                    )
                elif snooze_before is not None and snooze_before > tx.now:
                    task.last_spawned_slot = max(task.last_spawned_slot, tx.now)
                    pause_event = (EventType.RESUMED, {"skipped_until": at_text(tx.now, tx.tz)})
            after_untracked = {
                "title": task.title,
                "snooze_until": views.wire_value("snooze_until", task, tx.tz),
                "pattern_id": task.pattern_id,
                "parent_id": task.parent_id,
                "triaged": task.triaged,
            }
            untracked: dict[str, dict[str, Any]] = {}
            for name, before in before_untracked.items():
                if after_untracked[name] != before:
                    untracked[name] = {"from": before, "to": after_untracked[name]}
            # Long text is logged by length, not copied into the log.
            for name, before in before_text.items():
                after = getattr(task, name)
                if after != before:
                    untracked[name] = {
                        "from_chars": len(before or ""),
                        "to_chars": len(after or ""),
                    }
            changes = {**untracked, **write.changes}

            if changes:
                task.updated_at = tx.now
                record(
                    s, EventType.UPDATED, at=tx.now, task_id=task.id, payload={"changes": changes}
                )
            if pause_event is not None:
                record(s, pause_event[0], at=tx.now, task_id=task.id, payload=pause_event[1])
            for conflict in write.conflicts:
                record(s, EventType.CONFLICT, at=tx.now, task_id=task.id, payload=conflict)
            if moving and new_parent is not None:
                self._flip_parent_achievable(tx, new_parent)
            s.flush()
            s.expire(task, ["children"])

            payload: dict[str, Any] = {"task": self._detail(tx, task), "skipped": write.skipped}
            if write.skipped:
                every = {item["field"]: "user" for item in write.skipped}
                payload["hint"] = SKIP_HINT.format(sources=json.dumps(every))
            return self._respond(tx, payload)

    # --- subdivide_task (8.4) ---

    def subdivide_task(
        self,
        parent_id: int,
        children: list[dict[str, Any]],
        sequential: bool = False,
        sources: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(children, list) or not 1 <= len(children) <= SUBDIVIDE_MAX_CHILDREN:
            raise Invalid(
                f"children must be a list of 1 to {SUBDIVIDE_MAX_CHILDREN} tasks.",
                hint='For example children=[{"title": "Draft the outline"}, {"title": "..."}].',
            )
        if not isinstance(sequential, bool):
            raise Invalid("sequential must be true or false.", hint="For example sequential=true.")
        with self.tx() as tx:
            s = tx.session
            parent = self._task(tx, parent_id, "parent task")
            if parent.status is not Status.OPEN:
                raise ParentClosed(parent.id, parent.status.value)
            if parent.is_recurring:
                raise Unsupported(
                    f"Task {parent.id} is recurring, so it cannot be broken down.",
                    hint="Subdivide one of its instances instead.",
                    task_id=parent.id,
                )
            src = wire.sources(sources)

            parsed: list[dict[str, Any]] = []
            afters: list[list[int]] = []
            for n, child in enumerate(children, start=1):
                parsed.append(self._child_fields(tx, n, child))
                afters.append(_positions(n, child.get("after"), len(children), sequential))

            created = [self._new_task(tx, values, src, parent_id=parent.id) for values in parsed]
            links: list[tuple[int, int]] = []
            if sequential:
                links = [(a.id, b.id) for a, b in zip(created, created[1:], strict=False)]
            for n, positions in enumerate(afters):
                links.extend((created[p - 1].id, created[n].id) for p in positions)
            self._check_cycle(tx, add_blocks=links)

            for child in created:
                record(
                    s,
                    EventType.CREATED,
                    at=tx.now,
                    task_id=child.id,
                    payload={"via": "subdivide", "parent_id": parent.id},
                )
            for blocker_id, blocked_id in links:
                s.add(TaskBlock(blocker_id=blocker_id, blocked_id=blocked_id, created_at=tx.now))
                record(
                    s,
                    EventType.LINKED,
                    at=tx.now,
                    task_id=blocked_id,
                    payload={"blocker_id": blocker_id, "blocked_id": blocked_id},
                )
            record(
                s,
                EventType.SUBDIVIDED,
                at=tx.now,
                task_id=parent.id,
                payload={"children": [c.id for c in created]},
            )
            self._flip_parent_achievable(tx, parent)
            snap = Snapshot(s, tx.now)
            return self._respond(
                tx,
                {
                    "parent": views.task_row(snap, parent, tx.tz),
                    "children": [views.task_row(snap, c, tx.tz) for c in created],
                },
            )

    def _child_fields(self, tx: Tx, n: int, child: Any) -> dict[str, Any]:
        if not isinstance(child, dict):
            raise Invalid(f"Child {n} must be an object.", hint='{"title": "..."}')
        unknown = sorted(set(child) - CHILD_KEYS)
        if unknown:
            raise Invalid(
                f"Child {n} has {', '.join(unknown)}, which a child cannot take.",
                hint=f"A child takes: {', '.join(sorted(CHILD_KEYS))}. Set other fields "
                "afterwards with update_task.",
            )
        given = {k: v for k, v in child.items() if k != "after"}
        given["title"] = given.get("title") or ""
        try:
            return self._parse_fields(tx, given)
        except OrchardError as err:
            raise Invalid(f"Child {n}: {err.message}", hint=err.hint) from None

    # --- link_tasks, unlink_tasks (8.5) ---

    def link_tasks(self, blocker_id: int, blocked_id: int) -> dict[str, Any]:
        with self.tx() as tx:
            s = tx.session
            blocker = self._task(tx, blocker_id, "blocker task")
            blocked = self._task(tx, blocked_id, "blocked task")
            if blocker.id == blocked.id:
                raise Invalid("A task cannot block itself.", hint="Pass two different task ids.")
            for task, doing in ((blocker, "use it as a blocker"), (blocked, "block it")):
                if task.status is not Status.OPEN:
                    raise NotOpen(task.id, task.status.value, doing=doing)
            _reject_recurring_blocker(blocker)
            already = s.get(TaskBlock, (blocker.id, blocked.id)) is not None
            if not already:
                self._check_cycle(tx, add_blocks=[(blocker.id, blocked.id)])
                s.add(TaskBlock(blocker_id=blocker.id, blocked_id=blocked.id, created_at=tx.now))
                record(
                    s,
                    EventType.LINKED,
                    at=tx.now,
                    task_id=blocked.id,
                    payload={"blocker_id": blocker.id, "blocked_id": blocked.id},
                )
            return self._link_response(tx, blocker, blocked, already_linked=already)

    def unlink_tasks(self, blocker_id: int, blocked_id: int) -> dict[str, Any]:
        with self.tx() as tx:
            s = tx.session
            blocker = self._task(tx, blocker_id, "blocker task")
            blocked = self._task(tx, blocked_id, "blocked task")
            link = s.get(TaskBlock, (blocker.id, blocked.id))
            if link is not None:
                s.delete(link)
                record(
                    s,
                    EventType.UNLINKED,
                    at=tx.now,
                    task_id=blocked.id,
                    payload={"blocker_id": blocker.id, "blocked_id": blocked.id},
                )
            extra = {} if link is not None else {"was_linked": False}
            return self._link_response(tx, blocker, blocked, **extra)

    def _link_response(self, tx: Tx, blocker: Task, blocked: Task, **extra: Any) -> dict[str, Any]:
        snap = Snapshot(tx.session, tx.now)
        payload = {
            "blocker": views.task_row(snap, blocker, tx.tz),
            "blocked": views.task_row(snap, blocked, tx.tz),
            **extra,
        }
        return self._respond(tx, payload, keep=[k for k in extra if k == "was_linked"])

    # --- start_task (8.6) ---

    def start_task(self, task_id: int) -> dict[str, Any]:
        with self.tx() as tx:
            task = self._task(tx, task_id)
            if task.status is not Status.OPEN:
                raise NotOpen(task.id, task.status.value, doing="start it")
            if task.recur_mode is RecurMode.SCHEDULED:
                raise Invalid(
                    f"Task {task.id} is scheduled; its occurrences are what get worked on.",
                    hint="Start its open occurrence instead (get_task shows its children).",
                )
            record(tx.session, EventType.STARTED, at=tx.now, task_id=task.id, payload={})
            snap = Snapshot(tx.session, tx.now)
            return self._respond(
                tx,
                {
                    "task": views.task_detail(snap, task, tx.tz),
                    "starts": snap.counts(task).starts,
                },
            )

    # --- complete_task (8.7) ---

    def complete_task(
        self,
        task_id: int | None = None,
        *,
        title: str | None = None,
        parent_id: int | None = None,
        motive: str | None = None,
        description: str | None = None,
        completed_at: str | None = None,
        actual_minutes: int | None = None,
        felt_activation: str | None = None,
        felt_drain: str | None = None,
        reflection: str | None = None,
        pattern_id: int | None = None,
        new_pattern: str | None = None,
        drop_open_children: bool = False,
    ) -> dict[str, Any]:
        if (task_id is None) == (title is None):
            raise Invalid(
                "Pass exactly one of task_id and title.",
                hint="task_id for a task that exists; title to record work that was never a task.",
            )
        if task_id is not None:
            extra = [
                k
                for k, v in (
                    ("parent_id", parent_id), ("motive", motive), ("description", description)
                )
                if v is not None
            ]  # fmt: skip
            if extra:
                raise Invalid(
                    f"{', '.join(extra)} can only be given with title.",
                    hint="To change an existing task's fields, use update_task.",
                )
        if pattern_id is not None and new_pattern is not None:
            raise Invalid(
                "Pass pattern_id or new_pattern, not both.",
                hint="Use pattern_id for an existing pattern, new_pattern to name a new one.",
            )
        if actual_minutes is not None and (
            isinstance(actual_minutes, bool)
            or not isinstance(actual_minutes, int)
            or actual_minutes < 0
        ):
            raise Invalid(
                "actual_minutes must be a whole number, 0 or more.",
                hint="For example actual_minutes=40.",
            )
        if not isinstance(drop_open_children, bool):
            raise Invalid(
                "drop_open_children must be true or false.",
                hint="For example drop_open_children=true.",
            )
        felt = {
            name: wire.parse_enum(Level, value, name)
            for name, value in (("felt_activation", felt_activation), ("felt_drain", felt_drain))
            if value is not None
        }
        if reflection is not None:
            reflection = wire.text(reflection, "reflection").strip() or None
        pattern_name = None if new_pattern is None else _pattern_name(new_pattern, "new_pattern")

        with self.tx() as tx:
            s = tx.session
            when = tx.now
            if completed_at is not None:
                when = parse_when(completed_at, tx.tz, tx.now, DayMeans.COMPLETION)
                if when > tx.now:
                    raise Invalid(
                        "completed_at is in the future.",
                        hint="Use when the work was actually done, or leave it out for now.",
                    )
            values = self._parse_fields(
                tx,
                {
                    "title": title,
                    "motive": motive,
                    "description": description,
                    "pattern_id": pattern_id,
                },
            )

            if title is not None:
                parent = None
                if parent_id is not None:
                    parent = self._task(tx, parent_id, "parent task")
                    if parent.is_recurring:
                        raise Invalid(
                            f"Task {parent.id} is recurring.",
                            hint="To record one occurrence, pass it as task_id instead.",
                        )
                    if parent.status is not Status.OPEN:
                        raise ParentClosed(parent.id, parent.status.value)
                task = self._new_task(tx, values, {}, parent_id=parent_id)
                record(
                    s,
                    EventType.CREATED,
                    at=tx.now,
                    task_id=task.id,
                    payload={"via": "complete", "parent_id": parent_id},
                )
                if parent is not None:
                    self._flip_parent_achievable(tx, parent)
            else:
                task = self._task(tx, task_id)
                if task.status is not Status.OPEN:
                    raise NotOpen(task.id, task.status.value, doing="complete it")
                if task.is_recurring:
                    task = self._occurrence_to_complete(tx, task)

            before = Snapshot(s, tx.now)
            open_children = before.open_children.get(task.id, [])
            if open_children and not drop_open_children:
                shown = open_children[: views.DETAIL_CHILDREN]
                raise HasOpenChildren(task.id, [views.ref(before.task(c)) for c in shown])
            found_motive = before.effective_motive(task)

            if pattern_name is not None:
                task.pattern_id = self._pattern_named(tx, pattern_name).id
            elif "pattern_id" in values:
                task.pattern_id = values["pattern_id"]
            dropped = self._drop_descendants(tx, before, task, "parent completed")

            estimate_source = None
            if task.estimate_minutes is not None:
                estimate_source = FieldSource(
                    task.field_sources.get("estimate_minutes", FieldSource.GUESS.value)
                )
            s.add(
                Completion(
                    task_id=task.id,
                    completed_at=when,
                    actual_minutes=actual_minutes,
                    felt_activation=felt.get("felt_activation"),
                    felt_drain=felt.get("felt_drain"),
                    reflection=reflection,
                    estimate_at_completion=task.estimate_minutes,
                    estimate_source_at_completion=estimate_source,
                    motive_at_completion=None if found_motive is None else found_motive.value,
                )
            )
            self._close(tx, task, Status.DONE)
            payload: dict[str, Any] = {"completed_at": at_text(when, tx.tz)}
            if actual_minutes is not None:
                payload["actual_minutes"] = actual_minutes
            if pattern_id is not None or pattern_name is not None:
                payload["pattern_id"] = task.pattern_id
            recurring = None
            if task.parent_id is not None:
                recurring = s.get(Task, task.parent_id)
                if not recurring.is_recurring:
                    recurring = None
            if recurring is not None:
                payload["recurring_id"] = recurring.id
                if recurring.recur_mode is RecurMode.FLEXIBLE and recurring.gap_days:
                    recurring.snooze_until = day_snooze_end(when, recurring.gap_days, tx.tz)
                    recurring.updated_at = tx.now
                    payload["sleeping_until"] = at_text(recurring.snooze_until, tx.tz)
            record(s, EventType.COMPLETED, at=tx.now, task_id=task.id, payload=payload)

            after = Snapshot(s, tx.now)
            unblocked, more = self._unblocked(
                tx, before, after, {task.id, *(d.id for d in dropped)}
            )
            response: dict[str, Any] = {
                "completed": views.task_row(after, task, tx.tz),
                "parent_check": self._parent_check(tx, after, task.parent_id),
                "unblocked": unblocked,
                "unblocked_more": more,
            }
            if recurring is not None and recurring.recur_mode is RecurMode.SCHEDULED:
                upcoming = recur.next_slot(recurring, recurring.last_spawned_slot, tx.tz)
                if upcoming is not None:
                    response["next_slot"] = stamp(upcoming, tx.tz, tx.now)
            elif recurring is not None:
                response["recurrence_check"] = self._recurrence_check(tx, after, recurring)
            return self._respond(tx, response)

    def _occurrence_to_complete(self, tx: Tx, recurring: Task) -> Task:
        """Completing a recurring task completes one occurrence (SPEC 7.1, 7.2):
        the oldest open instance, or a new one made for the purpose."""
        s = tx.session
        live = recur.open_instances(s, recurring)
        if live:
            return live[0]
        if recurring.recur_mode is RecurMode.SCHEDULED:
            slot = recur.next_slot(recurring, recurring.last_spawned_slot, tx.tz)
            if slot is None:
                raise Invalid(
                    f"Task {recurring.id}'s schedule has no dates left.",
                    hint="Record the work with complete_task(title=...) instead.",
                )
            instance = recur.make_instance(s, recurring, tx.now, slot=slot, triaged=True)
            recurring.last_spawned_slot = slot
            record(
                s,
                EventType.SPAWNED,
                at=tx.now,
                task_id=instance.id,
                payload={"recurring_id": recurring.id, "occurrence_at": at_text(slot, tx.tz)},
            )
            return instance
        instance = recur.make_instance(s, recurring, tx.now, slot=None, triaged=True)
        record(
            s,
            EventType.CREATED,
            at=tx.now,
            task_id=instance.id,
            payload={"via": "complete", "parent_id": recurring.id},
        )
        return instance

    @staticmethod
    def _recurrence_check(tx: Tx, after: Snapshot, recurring: Task) -> dict[str, Any]:
        """What a flexible task's completion shows the agent (SPEC 7.2)."""
        times = history.recent_completions(tx.session, recurring.id, RECENT_COMPLETIONS + 1)
        gaps = [
            round((a - b).total_seconds() / 86_400, 1)
            for a, b in zip(times, times[1:], strict=False)
        ]
        sleeping = None
        if recurring.snooze_until is not None and recurring.snooze_until > tx.now:
            sleeping = stamp(recurring.snooze_until, tx.tz, tx.now)
        return views.compact(
            {
                "recurring": views.task_row(after, recurring, tx.tz),
                "cadence": recurring.cadence,
                "gap_days": recurring.gap_days,
                "sleeping_until": sleeping,
                "recent": [{"when": relative(t, tx.now)} for t in times[:RECENT_COMPLETIONS]],
                "gaps_days": gaps,
                "note": RECURRENCE_NOTE,
            }
        )

    def _pattern_named(self, tx: Tx, name: str) -> Pattern:
        """The pattern with this name, ignoring case, created if there is none."""
        found = tx.session.scalar(sa.select(Pattern).where(Pattern.name == name))
        if found is not None:
            return found
        pattern = Pattern(name=name, created_at=tx.now, updated_at=tx.now)
        tx.session.add(pattern)
        tx.session.flush()
        record(
            tx.session,
            EventType.PATTERN_SAVED,
            at=tx.now,
            payload={
                "pattern_id": pattern.id,
                "created": True,
                "changes": {"name": {"from": None, "to": name}},
            },
        )
        return pattern

    # --- snooze_task (8.8) ---

    def snooze_task(
        self,
        task_id: int,
        until: str | None = None,
        days: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if (until is None) == (days is None):
            raise Invalid(
                "Pass exactly one of until and days.",
                hint='For example until="friday" or days=3. days=0 wakes the task.',
            )
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 0):
            raise Invalid(
                "days must be a whole number, 0 or more.",
                hint="For example days=3, or days=0 to wake the task now.",
            )
        if reason is not None:
            reason = wire.text(reason, "reason").strip() or None
        with self.tx() as tx:
            s = tx.session
            task = self._task(tx, task_id)
            if task.status is not Status.OPEN:
                raise NotOpen(task.id, task.status.value, doing="snooze it")
            if until is not None:
                target = parse_when(until, tx.tz, tx.now, DayMeans.SNOOZE)
            elif days == 0:
                target = tx.now
            else:
                target = day_snooze_end(tx.now, days, tx.tz)
            previous = None if task.snooze_until is None else at_text(task.snooze_until, tx.tz)
            if task.recur_mode is RecurMode.SCHEDULED:
                # A scheduled task's own snooze is a pause (SPEC 7.1).
                if target > tx.now:
                    task.snooze_until = target
                    task.updated_at = tx.now
                    record(
                        s,
                        EventType.PAUSED,
                        at=tx.now,
                        task_id=task.id,
                        payload=views.compact({"until": at_text(target, tx.tz), "reason": reason}),
                    )
                elif task.snooze_until is not None:
                    recur.resume(s, task, tx.now, tx.now, tx.tz)
            elif target <= tx.now:
                if task.snooze_until is not None:
                    task.snooze_until = None
                    task.updated_at = tx.now
                    record(
                        s, EventType.WOKEN, at=tx.now, task_id=task.id,
                        payload={"previous": previous},
                    )  # fmt: skip
            else:
                task.snooze_until = target
                task.updated_at = tx.now
                record(
                    s,
                    EventType.SNOOZED,
                    at=tx.now,
                    task_id=task.id,
                    payload=views.compact(
                        {
                            "until": at_text(target, tx.tz),
                            "reason": reason,
                            "previous": previous,
                        }
                    ),
                )
            return self._respond(tx, {"task": self._detail(tx, task)})

    # --- drop_tasks (8.9) ---

    def drop_tasks(self, task_ids: list[int], reason: str) -> dict[str, Any]:
        ids = wire.ids(task_ids, "task_ids")
        if not 1 <= len(ids) <= DROP_MAX_TASKS:
            raise Invalid(
                f"task_ids must list 1 to {DROP_MAX_TASKS} tasks.",
                hint="For example task_ids=[12, 15].",
            )
        if not isinstance(reason, str) or not reason.strip():
            raise Invalid(
                "reason must be non-blank text.",
                hint='Say why, in the user\'s words if possible: reason="no longer needed".',
            )
        reason = reason.strip()
        with self.tx() as tx:
            s = tx.session
            tasks = [self._task(tx, i) for i in ids]
            listed = [t for t in tasks if t.status is Status.OPEN]
            listed_ids = frozenset(t.id for t in listed)
            skipped = [
                {**views.ref(t), "status": t.status.value}
                for t in tasks
                if t.status is not Status.OPEN
            ]

            before = Snapshot(s, tx.now)
            also: list[Task] = []
            for task in listed:
                self._close(tx, task, Status.DROPPED)
                record(s, EventType.DROPPED, at=tx.now, task_id=task.id, payload={"reason": reason})
                also.extend(self._drop_descendants(tx, before, task, reason, skip=listed_ids))

            closed = {t.id for t in (*listed, *also)}
            after = Snapshot(s, tx.now)
            parents = sorted({t.parent_id for t in (*listed, *also) if t.parent_id} - closed)
            checks = [self._parent_check(tx, after, p) for p in parents]
            unblocked, more = self._unblocked(tx, before, after, closed)
            return self._respond(
                tx,
                {
                    "dropped": [views.ref(t) for t in listed],
                    "also_dropped": [views.ref(t) for t in also],
                    "skipped": skipped,
                    "parent_checks": [c for c in checks if c is not None],
                    "unblocked": unblocked,
                    "unblocked_more": more,
                },
                keep=["dropped"],
            )

    # --- reopen_task (8.10) ---

    def reopen_task(self, task_id: int) -> dict[str, Any]:
        with self.tx() as tx:
            s = tx.session
            task = self._task(tx, task_id)
            if task.status is Status.OPEN:
                raise AlreadyOpen(task.id)
            note = None
            if task.parent_id is not None:
                parent = s.get(Task, task.parent_id)
                if parent.status is not Status.OPEN:
                    raise ParentClosed(parent.id, parent.status.value)
                if parent.is_recurring:
                    note = self._check_reopen_instance(tx, parent)
            if task.recur_mode is RecurMode.SCHEDULED:
                # Slots that passed while it was retired are skipped (SPEC 8.10).
                task.last_spawned_slot = max(task.last_spawned_slot, tx.now)
            payload: dict[str, Any] = {"was": task.status.value}
            done = task.completion
            if done is not None:
                payload["completion"] = views.compact(
                    {
                        "completed_at": at_text(done.completed_at, tx.tz),
                        "actual_minutes": done.actual_minutes,
                        "felt_activation": views.wire_name(done.felt_activation),
                        "felt_drain": views.wire_name(done.felt_drain),
                        "reflection": done.reflection,
                        "estimate_at_completion": done.estimate_at_completion,
                        "estimate_source_at_completion": views.wire_name(
                            done.estimate_source_at_completion
                        ),
                        "motive_at_completion": views.wire_name(done.motive_at_completion),
                    },
                    keep=["actual_minutes"] if done.actual_minutes == 0 else [],
                )
                s.delete(done)
                s.flush()
                s.expire(task, ["completion"])
            task.status = Status.OPEN
            task.closed_at = None
            task.updated_at = tx.now
            record(s, EventType.REOPENED, at=tx.now, task_id=task.id, payload=payload)
            return self._respond(tx, {"task": self._detail(tx, task), "note": note})

    @staticmethod
    def _check_reopen_instance(tx: Tx, recurring: Task) -> str | None:
        """The instance rules of SPEC 8.10. Returns the note about a gap snooze."""
        live = recur.open_instances(tx.session, recurring)
        if recurring.recur_mode is RecurMode.SCHEDULED:
            if len(live) >= recur.INSTANCE_CAP:
                raise InstanceCap(recurring.id, [t.id for t in live])
            return None
        if live:
            raise InstanceExists(recurring.id, live[0].id)
        if recurring.snooze_until is not None and recurring.snooze_until > tx.now:
            return (
                f"Task {recurring.id} is still sleeping until "
                f"{at_text(recurring.snooze_until, tx.tz)} from the completion just undone. "
                "Wake it with snooze_task(days=0) if it should be offered now."
            )
        return None

    # --- get_task, get_tree, search_tasks (8.12) ---

    def get_task(self, task_id: int) -> dict[str, Any]:
        with self.tx() as tx:
            return self._respond(tx, {"task": self._detail(tx, self._task(tx, task_id))})

    def get_tree(
        self, root_id: int | None = None, depth: int = 2, include_closed: bool = False
    ) -> dict[str, Any]:
        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 5:
            raise Invalid("depth must be 1 to 5.", hint="For example depth=2.")
        with self.tx() as tx:
            s = tx.session
            rows = s.execute(sa.select(Task.id, Task.parent_id, Task.status)).all()
            status = {i: st for i, _, st in rows}

            def shown(i: int) -> bool:
                return include_closed or status[i] is Status.OPEN

            def order(ids: list[int]) -> list[int]:
                return sorted(ids, key=lambda i: (status[i] is not Status.OPEN, i))

            children: dict[int, list[int]] = {}
            for i, p, _ in rows:
                if p is not None and shown(i):
                    children.setdefault(p, []).append(i)
            if root_id is not None:
                roots = [self._task(tx, root_id).id]
            else:
                roots = order([i for i, p, _ in rows if p is None and shown(i)])

            # Breadth-first, so that a cut keeps whole upper levels.
            included: dict[int, int] = {}  # id -> level (1 for roots)
            queue = [(r, 1) for r in roots]
            truncated = False
            while queue:
                node, level = queue.pop(0)
                if len(included) >= TREE_MAX_NODES:
                    truncated = True
                    break
                included[node] = level
                if level < depth:
                    queue.extend((c, level + 1) for c in order(children.get(node, [])))

            def total_below(i: int) -> int:
                return sum(1 + total_below(c) for c in children.get(i, []))

            snap = Snapshot(s, tx.now)

            def build(i: int) -> dict[str, Any]:
                task = snap.task(i)
                kids = [build(c) for c in order(children.get(i, [])) if c in included]
                shown_below = sum(1 + k.get("_below", 0) for k in kids)
                for k in kids:
                    k.pop("_below", None)
                node = views.compact(
                    {
                        "id": i,
                        "title": views.truncate(task.title, views.ROW_TITLE_CHARS),
                        "status": None if task.status is Status.OPEN else task.status.value,
                        "recurring": task.recur_mode.value if task.recur_mode else None,
                        "state": snap.state(task),
                        "children": kids,
                        "more": total_below(i) - shown_below,
                    },
                    keep=("id", "title"),
                )
                node["_below"] = shown_below
                return node

            tree = [build(r) for r in roots if r in included]
            for node in tree:
                node.pop("_below", None)
            return self._respond(tx, {"tree": tree, "truncated": truncated}, keep=["tree"])

    def search_tasks(
        self,
        query: str | None = None,
        status: str = "open",
        inbox_only: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SEARCH_MAX:
            raise Invalid(f"limit must be 1 to {SEARCH_MAX}.", hint="For example limit=10.")
        which = wire.names(status, ("open", "closed", "any"), "status")
        if len(which) != 1:
            raise Invalid("status must be one of: open, closed, any.", hint='status="open"')
        which_status = which[0]
        with self.tx() as tx:
            s = tx.session
            filters = []
            if which_status == "open":
                filters.append(Task.status == Status.OPEN)
            elif which_status == "closed":
                filters.append(Task.status != Status.OPEN)
            if inbox_only:
                filters.append(Task.triaged.is_(False))

            # A blank query is treated as no query (SPEC 13).
            if query is not None and query.strip():
                words = _search_words(query)
                ids, total = _fts_search(s, words, filters, limit)
            else:
                stmt = sa.select(Task.id).where(*filters)
                total = s.scalar(sa.select(sa.func.count()).select_from(stmt.subquery()))
                ids = list(
                    s.scalars(stmt.order_by(Task.updated_at.desc(), Task.id.desc()).limit(limit))
                )
            snap = Snapshot(s, tx.now)
            results = [views.task_row(snap, snap.task(i), tx.tz, always_status=True) for i in ids]
            return self._respond(
                tx, {"results": results, "total": total}, keep=["results", "total"]
            )

    # --- get_candidates, get_critical (8.13, 8.14) ---

    def get_candidates(
        self,
        limit: int = 20,
        under_id: int | None = None,
        min_priority: str | None = None,
        due_within_days: float | None = None,
        minutes_available: int | None = None,
        exclude_conditions: list[str] | None = None,
        energy: str | None = None,
        reserve_wants: int = 3,
    ) -> dict[str, Any]:
        _int_in_range(limit, "limit", 1, CANDIDATES_MAX)
        _int_in_range(reserve_wants, "reserve_wants", 0, RESERVE_WANTS_MAX)
        priority = (
            None
            if min_priority is None
            else wire.parse_enum(Priority, min_priority, "min_priority")
        )
        level = None if energy is None else wire.parse_enum(Level, energy, "energy")
        within = None
        if due_within_days is not None:
            within = timedelta(
                days=_number_in_range(due_within_days, "due_within_days", 0, DUE_WITHIN_MAX_DAYS)
            )
        if minutes_available is not None:
            wire.positive_int(minutes_available, "minutes_available")
        excluded = None
        if exclude_conditions is not None:
            excluded = frozenset(wire.conditions(exclude_conditions))
        with self.tx() as tx:
            snap = Snapshot(tx.session, tx.now)
            under = None
            if under_id is not None:
                root = self._task(tx, under_id)
                under = frozenset([root.id, *snap.open_descendants(root.id)])
            filters = shortlist.Filters(
                under=under,
                min_priority=priority,
                due_within=within,
                minutes_available=minutes_available,
                exclude_conditions=excluded,
            )
            payload = shortlist.candidates(
                snap, tx.tz, limit=limit, filters=filters, energy=level, reserve_wants=reserve_wants
            )
            return self._respond(
                tx, payload, keep=["candidates", "pool", "conditions_in_pool", "inbox"]
            )

    def get_critical(self, horizon_days: float = 1) -> dict[str, Any]:
        days = _number_in_range(horizon_days, "horizon_days", 0, HORIZON_MAX_DAYS)
        with self.tx() as tx:
            snap = Snapshot(tx.session, tx.now)
            payload = shortlist.critical(snap, tx.tz, horizon=timedelta(days=days))
            return self._respond(tx, payload, keep=["critical", "inbox"])

    # --- merge_tasks (8.11) ---

    def merge_tasks(self, source_id: int, into_id: int) -> dict[str, Any]:
        with self.tx() as tx:
            s = tx.session
            source = self._task(tx, source_id, "source task")
            into = self._task(tx, into_id, "target task")
            if source.id == into.id:
                raise Invalid(
                    "A task cannot be merged into itself.",
                    hint="Pass the duplicate as source_id and the task to keep as into_id.",
                )
            for task in (source, into):
                if task.status is not Status.OPEN:
                    raise NotOpen(task.id, task.status.value, doing="merge it")
                self._reject_recurring_merge(tx, task)
            structure = self._structure(tx)
            if into.id in graph.ancestors(source.id, structure.parent) or source.id in (
                graph.ancestors(into.id, structure.parent)
            ):
                raise Invalid(
                    f"Tasks {source.id} and {into.id} are in the same branch.",
                    hint="Merge only separate tasks. To fold a child into its parent, "
                    "complete or drop the child instead.",
                )

            children = list(
                s.scalars(sa.select(Task).where(Task.parent_id == source.id).order_by(Task.id))
            )
            old_links = {(a, b) for a, b in structure.blocks if source.id in (a, b)}
            new_links = set()
            for blocker, blocked in old_links:
                link = (
                    into.id if blocker == source.id else blocker,
                    into.id if blocked == source.id else blocked,
                )
                if link[0] != link[1] and link not in structure.blocks:
                    new_links.add(link)
            structure.blocks = (structure.blocks - old_links) | new_links
            for child in children:
                structure.parent[child.id] = into.id
            steps = graph.check(structure)
            if steps is not None:
                self._raise_cycle(tx, steps)

            before = Snapshot(s, tx.now)
            for child in children:
                child.parent_id = into.id
                child.updated_at = tx.now
            s.execute(
                sa.delete(TaskBlock).where(
                    sa.or_(TaskBlock.blocker_id == source.id, TaskBlock.blocked_id == source.id)
                )
            )
            for blocker, blocked in sorted(new_links):
                s.add(TaskBlock(blocker_id=blocker, blocked_id=blocked, created_at=tx.now))

            changes, filled = self._fill_from(tx, into, source)
            old_notes = into.notes or ""
            into.notes = _merged_notes(old_notes, source)
            changes["notes"] = {"from_chars": len(old_notes), "to_chars": len(into.notes)}
            if any(before.is_open(c.id) for c in children) and into.achievable is not (
                Achievable.NO
            ):
                was = changes.get("achievable", {}).get("from", into.achievable.value)
                into.achievable = Achievable.NO
                into.field_sources["achievable"] = FieldSource.EVIDENCE.value
                changes["achievable"] = {
                    "from": None if was == Achievable.UNKNOWN.value else was,
                    "to": Achievable.NO.value,
                    "source": FieldSource.EVIDENCE.value,
                }
            into.updated_at = tx.now
            self._close(tx, source, Status.DROPPED)

            record(
                s,
                EventType.MERGED,
                at=tx.now,
                task_id=source.id,
                payload={
                    "into": into.id,
                    "moved_children": [c.id for c in children],
                    "moved_links": len(new_links),
                    "filled": filled,
                },
            )
            record(s, EventType.UPDATED, at=tx.now, task_id=into.id, payload={"changes": changes})
            return self._respond(tx, {"task": self._detail(tx, into)})

    @staticmethod
    def _reject_recurring_merge(tx: Tx, task: Task) -> None:
        parent = None if task.parent_id is None else tx.session.get(Task, task.parent_id)
        if task.is_recurring or (parent is not None and parent.is_recurring):
            raise Unsupported(
                f"Task {task.id} is recurring or an occurrence of one, so it cannot be merged.",
                hint="Drop the extra occurrence instead, or merge plain tasks only.",
                task_id=task.id,
            )

    @staticmethod
    def _fill_from(tx: Tx, into: Task, source: Task) -> tuple[dict[str, Any], list[str]]:
        """Copy every tracked field the target lacks and the source has (SPEC 8.11)."""
        changes: dict[str, Any] = {}
        filled: list[str] = []
        for name in TRACKED_FIELDS:
            if views.wire_value(name, into, tx.tz) is not None:
                continue
            value = views.wire_value(name, source, tx.tz)
            if value is None:
                continue
            if name == "deadline":
                into.deadline = source.deadline
                into.deadline_firmness = source.deadline_firmness
            elif name == "conditions":
                into.conditions = list(source.conditions)
            else:
                setattr(into, name, getattr(source, name))
            origin = source.field_sources.get(name, FieldSource.GUESS.value)
            into.field_sources[name] = origin
            changes[name] = {"from": None, "to": value, "source": origin}
            filled.append(name)
        if into.pattern_id is None and source.pattern_id is not None:
            into.pattern_id = source.pattern_id
            changes["pattern_id"] = {"from": None, "to": source.pattern_id}
            filled.append("pattern_id")
        return changes, filled

    # --- get_review_queue (8.15), get_recent_activity (8.16) ---

    def get_review_queue(self) -> dict[str, Any]:
        with self.tx() as tx:
            snap = Snapshot(tx.session, tx.now)
            queue = review.review_queue(snap, tx.tz)
            return self._respond(tx, queue, keep=list(queue))

    def get_recent_activity(self, days: int = review.ACTIVITY_DEFAULT_DAYS) -> dict[str, Any]:
        days = _int_in_range(days, "days", 1, review.ACTIVITY_DAYS_MAX)
        with self.tx() as tx:
            activity = review.recent_activity(tx.session, tx.now, days)
            return self._respond(tx, activity, keep=["balance"])

    # --- find_patterns, save_pattern (8.17) ---

    def find_patterns(self, query: str | None = None, limit: int = 5) -> dict[str, Any]:
        _int_in_range(limit, "limit", 1, PATTERNS_MAX)
        with self.tx() as tx:
            s = tx.session
            if query is not None and query.strip():
                ids, _ = _fts_search(s, _search_words(query), [], limit, Pattern)
                patterns = [s.get(Pattern, i) for i in ids]
            else:
                # Most recently used first; never-used patterns last, newest first.
                used = history.latest_completion_by_pattern(s)
                patterns = sorted(
                    s.scalars(sa.select(Pattern)),
                    key=lambda p: (p.id in used, used.get(p.id, tx.now), p.id),
                    reverse=True,
                )[:limit]
            return self._respond(
                tx,
                {"patterns": views.pattern_digests(s, patterns, tx.tz, tx.now)},
                keep=["patterns"],
            )

    def save_pattern(
        self,
        pattern_id: int | None = None,
        name: str | None = None,
        description: str | None = None,
        learned: str | None = None,
        merge_from_id: int | None = None,
    ) -> dict[str, Any]:
        # Blank description or learned clears it, so None means "not given".
        given: dict[str, str | None] = {}
        if name is not None:
            given["name"] = _pattern_name(name, "name")
        if description is not None:
            given["description"] = wire.text(description, "description").strip() or None
            limit = views.PATTERN_DESCRIPTION_MAX_CHARS
            if given["description"] and len(given["description"]) > limit:
                raise Invalid(
                    f"description is {len(given['description'])} characters; the limit is {limit}.",
                    hint="Say briefly what counts as this kind of work. What has been learned "
                    "about it goes in learned.",
                )
        if learned is not None:
            text = wire.text(learned, "learned").strip()
            if len(text) > LEARNED_MAX_CHARS:
                raise Invalid(
                    f"learned is {len(text)} characters; the limit is {LEARNED_MAX_CHARS}.",
                    hint="Rewrite the whole note shorter, keeping what matters most. Do not "
                    "append to it.",
                )
            given["learned"] = text or None
        if pattern_id is None and merge_from_id is not None:
            raise Invalid(
                "merge_from_id needs pattern_id: the pattern to merge into.",
                hint="Pass pattern_id for the pattern that should remain.",
            )
        if pattern_id is None and "name" not in given:
            raise Invalid(
                "A new pattern needs a name.",
                hint='For example name="Phone calls to book things".',
            )
        with self.tx() as tx:
            s = tx.session
            pattern = None if pattern_id is None else _get_pattern(tx, pattern_id)
            if merge_from_id is not None:
                self._merge_patterns(tx, pattern, _get_pattern(tx, merge_from_id))

            if "name" in given:
                clash = s.scalar(sa.select(Pattern).where(Pattern.name == given["name"]))
                if clash is not None and clash is not pattern:
                    raise Invalid(
                        f"A pattern named {clash.name!r} already exists.",
                        hint=f"Use pattern_id={clash.id}, or choose a different name.",
                        pattern_id=clash.id,
                    )
            created = pattern is None
            if created:
                pattern = Pattern(name=given["name"], created_at=tx.now, updated_at=tx.now)
                s.add(pattern)
                s.flush()
                changes: dict[str, Any] = {"name": {"from": None, "to": pattern.name}}
            else:
                changes = {}
                if "name" in given and given["name"] != pattern.name:
                    changes["name"] = {"from": pattern.name, "to": given["name"]}
                    pattern.name = given["name"]
            if "learned" in given and given["learned"] != pattern.learned:
                changes["learned"] = {"from": pattern.learned, "to": given["learned"]}
                pattern.learned = given["learned"]
            if "description" in given and given["description"] != pattern.description:
                changes["description"] = {
                    "from_chars": len(pattern.description or ""),
                    "to_chars": len(given["description"] or ""),
                }
                pattern.description = given["description"]
            if changes:
                pattern.updated_at = tx.now
                payload: dict[str, Any] = {"pattern_id": pattern.id, "changes": changes}
                if created:
                    payload = {"pattern_id": pattern.id, "created": True, "changes": changes}
                record(s, EventType.PATTERN_SAVED, at=tx.now, payload=payload)
            s.flush()
            return self._respond(tx, {"pattern": views.pattern_digest(s, pattern, tx.tz, tx.now)})

    @staticmethod
    def _merge_patterns(tx: Tx, into: Pattern, source: Pattern) -> None:
        """Move every task from `source` to `into`, then delete `source`."""
        s = tx.session
        if source.id == into.id:
            raise Invalid(
                "A pattern cannot be merged into itself.", hint="Pass two different pattern ids."
            )
        moved = s.execute(
            sa.update(Task).where(Task.pattern_id == source.id).values(pattern_id=into.id)
        ).rowcount
        record(
            s,
            EventType.PATTERNS_MERGED,
            at=tx.now,
            payload={
                "from": {"id": source.id, "name": source.name},
                "into": into.id,
                "tasks_moved": moved,
            },
        )
        s.delete(source)
        s.flush()

    # --- read_profile, write_profile_note (8.18) ---

    def _profile(self, tx: Tx) -> dict[str, Any]:
        rows = tx.session.scalars(sa.select(ProfileNote).order_by(sa.func.lower(ProfileNote.topic)))
        charter = None
        notes = []
        for note in rows:
            entry = {"text": note.text, "updated": stamp(note.updated_at, tx.tz, tx.now)}
            if _is_charter(note.topic):
                charter = entry
            else:
                notes.append({"topic": note.topic, **entry})
        used = sum(len(n["text"]) for n in notes)
        return self._respond(
            tx,
            {
                "charter": charter,
                "notes": notes,
                "size": {"used": used, "limit": NOTES_TOTAL_MAX_CHARS},
            },
            keep=["notes", "size"],
        )

    def read_profile(self) -> dict[str, Any]:
        with self.tx() as tx:
            return self._profile(tx)

    def write_profile_note(self, topic: str, text: str | None) -> dict[str, Any]:
        if not isinstance(topic, str) or not topic.strip():
            raise Invalid("topic must be non-blank text.", hint='For example topic="energy".')
        topic = " ".join(topic.split())
        if len(topic) > TOPIC_MAX_CHARS:
            raise Invalid(
                f"topic is {len(topic)} characters; the limit is {TOPIC_MAX_CHARS}.",
                hint="Use a short topic of a word or two, and put the detail in text.",
            )
        if text is not None and not isinstance(text, str):
            raise Invalid("text must be text.", hint="Pass blank text to delete the note.")
        text = (text or "").strip()
        with self.tx() as tx:
            s = tx.session
            existing = s.scalar(sa.select(ProfileNote).where(ProfileNote.topic == topic))
            if not text:
                if existing is not None:
                    s.delete(existing)
                    record(
                        s,
                        EventType.PROFILE_SAVED,
                        at=tx.now,
                        payload={"topic": existing.topic, "deleted": True},
                    )
                return self._profile(tx)

            # The current sizes, so the agent can see where to consolidate.
            others = sum(
                len(n.text)
                for n in s.scalars(sa.select(ProfileNote))
                if not _is_charter(n.topic) and n is not existing
            )
            charter = s.scalar(
                sa.select(ProfileNote).where(ProfileNote.topic == ProfileNote.CHARTER_TOPIC)
            )
            sizes = {
                "chars": len(text),
                "charter_used": len(charter.text) if charter else 0,
                "charter_limit": CHARTER_MAX_CHARS,
                "note_limit": NOTE_MAX_CHARS,
                "notes_used": others,
                "notes_limit": NOTES_TOTAL_MAX_CHARS,
            }
            if _is_charter(topic):
                if len(text) > CHARTER_MAX_CHARS:
                    raise ProfileFull(
                        f"The charter would be {len(text)} characters; the limit is "
                        f"{CHARTER_MAX_CHARS}.",
                        **sizes,
                    )
            else:
                if len(text) > NOTE_MAX_CHARS:
                    raise ProfileFull(
                        f"That note would be {len(text)} characters; the limit is "
                        f"{NOTE_MAX_CHARS} per note.",
                        **sizes,
                    )
                if others + len(text) > NOTES_TOTAL_MAX_CHARS:
                    raise ProfileFull(
                        f"The notes would total {others + len(text)} characters; the limit is "
                        f"{NOTES_TOTAL_MAX_CHARS}.",
                        **sizes,
                    )

            if existing is None:
                s.add(ProfileNote(topic=topic, text=text, created_at=tx.now, updated_at=tx.now))
            elif existing.text == text and existing.topic == topic:
                return self._profile(tx)
            else:
                existing.topic, existing.text, existing.updated_at = topic, text, tx.now
            s.flush()
            record(s, EventType.PROFILE_SAVED, at=tx.now, payload={"topic": topic})
            return self._profile(tx)


# --- Module helpers ---


def _positions(n: int, value: Any, count: int, sequential: bool) -> list[int]:
    """A child's `after`: positions (from 1) of other children in the same call."""
    if value is None:
        return []
    if sequential:
        raise Invalid(
            "after cannot be used together with sequential.",
            hint="Use sequential=true for a straight chain, or after for each child, not both.",
        )
    if isinstance(value, int) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        raise Invalid(f"Child {n}: after must be a list of positions.", hint="For example [1, 2].")
    out: list[int] = []
    for p in value:
        if isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= count or p == n:
            raise Invalid(
                f"Child {n}: after must list positions of other children, 1 to {count}.",
                hint="Positions count from 1 in the children list. A child cannot follow itself.",
            )
        if p not in out:
            out.append(p)
    return out


def _pattern_name(value: Any, param: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Invalid(f"{param} must be non-blank text.", hint='For example "Interface specs".')
    name = " ".join(value.split())
    if len(name) > PATTERN_NAME_MAX_CHARS:
        raise Invalid(
            f"{param} is {len(name)} characters; the limit is {PATTERN_NAME_MAX_CHARS}.",
            hint="Name the kind of work in a few words; put the rest in description.",
        )
    return name


def _get_pattern(tx: Tx, pattern_id: Any) -> Pattern:
    if isinstance(pattern_id, bool) or not isinstance(pattern_id, int):
        raise Invalid("pattern ids must be numbers.", hint="Use find_patterns to find one.")
    pattern = tx.session.get(Pattern, pattern_id)
    if pattern is None:
        raise NotFound("pattern", pattern_id)
    return pattern


def _reject_for_recurring(**given: Any) -> None:
    """A recurring task has no deadline and no achievable value (SPEC 7.3)."""
    bad = [name for name, value in given.items() if value is not None]
    if bad:
        raise Invalid(
            f"A recurring task cannot have {', '.join(bad)}.",
            hint=f"Call again without {', '.join(bad)}. A scheduled task's occurrences take "
            "their deadline from its rule and their firmness from recurrence.firmness.",
        )


def _reject_recurring_blocker(task: Task) -> None:
    if task.is_recurring:
        raise Unsupported(
            f"Task {task.id} is recurring, so it cannot block other tasks.",
            hint="Use one of its instances as the blocker instead.",
            task_id=task.id,
        )


# Letters and digits only: the tokenizer treats "_" as a separator, so a query
# of underscores would otherwise reach MATCH as a phrase with no words.
_WORD = re.compile(r"[^\W_]+")


def _no_words() -> Invalid:
    return Invalid(
        "The search has no words to look for.",
        hint="Search for words from the task's title, or leave query out to list recent tasks.",
    )


def _search_words(query: str) -> list[str]:
    words: list[str] = []
    for word in _WORD.findall(query.lower()):
        if word not in words:
            words.append(word)
    if not words:
        raise _no_words()
    return words[:SEARCH_MAX_WORDS]


def _fts_search(
    s: Session, words: list[str], filters: list[Any], limit: int, model: type = Task
) -> tuple[list[int], int]:
    """Every word quoted, all required; if that finds nothing, any of them.

    User text never reaches MATCH unquoted (SPEC 8.12). Results are ordered by
    relevance (bm25), then most recently updated.
    """
    quoted = [f'"{w}"' for w in words]  # words contain no quote characters
    table = f"{model.__tablename__}_fts"
    fts = sa.table(table, sa.column("rowid"))
    rank = sa.literal_column(f"bm25({table})")
    for expression in (" ".join(quoted), " OR ".join(quoted)):
        match = sa.text(f"{table} MATCH :q").bindparams(q=expression)
        base = (
            sa.select(model.id)
            .select_from(fts)
            .join(model, model.id == fts.c.rowid)
            .where(match, *filters)
        )
        total = s.scalar(sa.select(sa.func.count()).select_from(base.subquery()))
        if total:
            ids = list(
                s.scalars(
                    base.order_by(rank, model.updated_at.desc(), model.id.desc()).limit(limit)
                )
            )
            return ids, total
        if len(words) == 1:
            break
    return [], 0


def _is_charter(topic: str) -> bool:
    return topic.strip().lower() == ProfileNote.CHARTER_TOPIC


def _int_in_range(value: Any, param: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Invalid(
            f"{param} must be a whole number from {low} to {high}.",
            hint=f"For example {param}={max(low, min(high, 3))}.",
        )
    return value


def _number_in_range(value: Any, param: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        raise Invalid(
            f"{param} must be a number from {low} to {high}.",
            hint=f"For example {param}=1 or {param}=0.5.",
        )
    return float(value)


def _zone_named(name: Any) -> str:
    """The canonical IANA name for `name`, ignoring case."""
    if isinstance(name, str) and name.strip():
        found = _zone_names().get(name.strip().casefold())
        if found is not None:
            return found
    raise Invalid(
        f"{name!r} is not a timezone name.",
        hint='Use an IANA name such as "Europe/London" or "America/New_York".',
    )


@functools.cache
def _zone_names() -> dict[str, str]:
    return {zone.casefold(): zone for zone in zoneinfo.available_timezones()}


MERGED_CUT = "… (cut to fit; see #{id})"


def _merged_notes(notes: str, source: Task) -> str:
    """The target's notes with the source's title, description and notes added
    under a `Merged from` line, cut to fit the notes limit (SPEC 8.11)."""
    parts = [f"Merged from #{source.id}: {source.title}"]
    parts += [text for text in (source.description, source.notes) if text]
    added = "\n".join(parts)
    joined = f"{notes}\n\n{added}" if notes else added
    if len(joined) <= wire.NOTES_MAX_CHARS:
        return joined
    cut = MERGED_CUT.format(id=source.id)
    return joined[: wire.NOTES_MAX_CHARS - len(cut)] + cut
