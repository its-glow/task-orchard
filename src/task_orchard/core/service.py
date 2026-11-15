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

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from task_orchard.config import Settings
from task_orchard.core import graph, recurrence, views, wire
from task_orchard.core.clock import Clock, SystemClock
from task_orchard.core.derive import Snapshot
from task_orchard.core.enums import (
    Achievable,
    EventType,
    FieldSource,
    Firmness,
    Level,
    Motive,
    Priority,
    Status,
)
from task_orchard.core.errors import (
    CycleError,
    Invalid,
    NotFound,
    NotOpen,
    ParentClosed,
    Unsupported,
)
from task_orchard.core.events import record
from task_orchard.core.timefmt import DayMeans, at_text, parse_when
from task_orchard.db.engine import init_db, make_engine, make_session_factory
from task_orchard.db.models import Pattern, Task, TaskBlock

SKIP_HINT = (
    "Some fields were not changed because the user set them. If the user asked for this "
    "change, repeat the call with sources={sources}."
)
CHILDREN_HINT = (
    "A task with children is not one step. Add a child for the remaining work, or complete "
    "this task."
)
RECURRING_LATER_HINT = (
    "Recurring tasks cannot be changed by this version yet. Change one of its instances "
    "instead, or leave it as it is."
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
SEARCH_MAX_WORDS = 20
TREE_MAX_NODES = 100
DUPLICATES_PER_ITEM = 3


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
        if steps is None:
            return
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
    def _reject_recurring_target(task: Task, doing: str) -> None:
        # Phase 1-3: recurring tasks are read-only until Phase 4 (SPEC 11).
        if task.is_recurring:
            raise Unsupported(
                f"Task {task.id} is recurring, so you cannot {doing} yet.",
                hint=RECURRING_LATER_HINT,
                task_id=task.id,
            )

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
                (t.id, t.title, _normalise_title(t.title))
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
                key = _normalise_title(title)
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
            if recurrence is not None:
                raise Unsupported(
                    "Recurring tasks cannot be created by this version yet.",
                    hint="Create a plain task now; it can be made recurring later.",
                )
            src = wire.sources(sources)
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

            parent = None
            if parent_id is not None:
                parent = self._task(tx, parent_id, "parent task")
                self._open_parent(tx, parent)

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
            if "conditions" in values and values["conditions"]:
                task.conditions = values["conditions"]
                task.field_sources["conditions"] = src.get("conditions", FieldSource.GUESS).value
            if "achievable" in values:
                task.achievable = values["achievable"]
                task.field_sources["achievable"] = src.get("achievable", FieldSource.GUESS).value
            if "deadline" in values:
                task.deadline = values["deadline"]
                task.deadline_firmness = values.get("firmness", Firmness.SOFT)
                task.field_sources["deadline"] = src.get("deadline", FieldSource.GUESS).value
            s.add(task)
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
            s.flush()
            s.refresh(task)
            return self._respond(tx, {"task": self._detail(tx, task)})

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
            self._reject_recurring_target(task, "update it")
            if recurrence is not None:
                raise Unsupported(
                    "Converting a task to recurring is not supported by this version yet.",
                    hint="Leave the task as it is for now.",
                )
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
            for name in ("cadence", "gap_days"):
                if name in to_clear:
                    raise Invalid(
                        f"Task {task.id} is not recurring, so it has no {name}.",
                        hint=f"Remove {name} from clear.",
                    )

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


# --- Module helpers ---


def _reject_recurring_blocker(task: Task) -> None:
    if task.is_recurring:
        raise Unsupported(
            f"Task {task.id} is recurring, so it cannot block other tasks.",
            hint="Use one of its instances as the blocker instead.",
            task_id=task.id,
        )


_TITLE_PUNCTUATION = re.compile(r"^[\W_]+|[\W_]+$")


def _normalise_title(title: str) -> str:
    """For duplicate detection: case, spacing and surrounding punctuation ignored."""
    return _TITLE_PUNCTUATION.sub("", " ".join(title.casefold().split()))


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
    s: Session, words: list[str], filters: list[Any], limit: int
) -> tuple[list[int], int]:
    """Every word quoted, all required; if that finds nothing, any of them.

    User text never reaches MATCH unquoted (SPEC 8.12). Results are ordered by
    relevance (bm25), then most recently updated.
    """
    quoted = [f'"{w}"' for w in words]  # words contain no quote characters
    fts = sa.table("tasks_fts", sa.column("rowid"))
    rank = sa.literal_column("bm25(tasks_fts)")
    for expression in (" ".join(quoted), " OR ".join(quoted)):
        match = sa.text("tasks_fts MATCH :q").bindparams(q=expression)
        base = (
            sa.select(Task.id)
            .select_from(fts)
            .join(Task, Task.id == fts.c.rowid)
            .where(match, *filters)
        )
        total = s.scalar(sa.select(sa.func.count()).select_from(base.subquery()))
        if total:
            ids = list(
                s.scalars(base.order_by(rank, Task.updated_at.desc(), Task.id.desc()).limit(limit))
            )
            return ids, total
        if len(words) == 1:
            break
    return [], 0
