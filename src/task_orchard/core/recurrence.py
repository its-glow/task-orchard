"""Recurrence: rules, slots, the scheduled spawner and the recurrence object.

SPEC section 7. Only this module and complete_task create instances.

- A scheduled task's rule is an RRULE evaluated on NAIVE LOCAL wall-clock time
  from Task.recur_dtstart, so "Friday 17:00" stays 17:00 across daylight-saving
  changes. Each slot is localised and converted to UTC at the edge.
- roll_forward runs at the start of every request. It resumes ended pauses,
  spawns instances whose lead window has opened, closes the oldest of three
  live instances as missed, and retires tasks whose rule has run out. Calling
  it twice with the same `now` changes nothing the second time.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from dateutil.rrule import rrule, rrulestr
from sqlalchemy.orm import Session

from task_orchard.core import wire
from task_orchard.core.enums import Achievable, EventType, FieldSource, Firmness, RecurMode, Status
from task_orchard.core.errors import Invalid
from task_orchard.core.events import record
from task_orchard.core.timefmt import at_text, local_date, to_utc
from task_orchard.db.models import Task

INSTANCE_CAP = 2
SPAWN_LIMIT = 60
DEFAULT_DUE_TIME = time(23, 59)
DEFAULT_LEAD_DAYS = 1
FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
NEXT_SLOTS = 3

RULE_HINT = (
    'Use an RRULE such as "FREQ=WEEKLY;BYDAY=FR" or "FREQ=MONTHLY;BYMONTHDAY=-1" (the last '
    "day of the month). The time of day comes from due_time."
)
_UNTIL_Z = re.compile(r"^(\d{8})T(\d{6})Z$")


# --- The rule ---


def normalise_rrule(text: Any, tz: ZoneInfo) -> str:
    """Validate an RRULE value (SPEC 7.1) and return it as stored.

    A leading `RRULE:` is stripped; a UTC `UNTIL` is rewritten in local
    wall-clock time, because the rule is evaluated on naive local time.
    """
    if not isinstance(text, str) or not text.strip():
        raise Invalid("rrule must be non-blank text.", hint=RULE_HINT)
    value = text.strip()
    if value.upper().startswith("RRULE:"):
        value = value[len("RRULE:") :]
    if "\n" in value or "\r" in value or "DTSTART" in value.upper():
        raise Invalid("rrule must be a single RRULE value, without DTSTART.", hint=RULE_HINT)
    parts: list[tuple[str, str]] = []
    for part in value.split(";"):
        if not part.strip():
            continue
        key, sep, val = part.partition("=")
        if not sep:
            raise Invalid(f"rrule part {part!r} is not KEY=VALUE.", hint=RULE_HINT)
        parts.append((key.strip().upper(), val.strip()))
    keys = [k for k, _ in parts]
    freq = dict(parts).get("FREQ", "").upper()
    if freq not in FREQUENCIES:
        raise Invalid(
            f"rrule FREQ must be one of {', '.join(FREQUENCIES)}.",
            hint="Something more often than daily is not a schedule this supports. " + RULE_HINT,
        )
    timed = [k for k in keys if k in ("BYHOUR", "BYMINUTE", "BYSECOND")]
    if timed:
        raise Invalid(
            f"rrule cannot use {', '.join(timed)}.",
            hint="Set the time of day with due_time. For two times a day, create two "
            "recurring tasks.",
        )
    out: list[str] = []
    for key, val in parts:
        if key == "UNTIL":
            val = _local_until(val, tz)
        out.append(f"{key}={val.upper() if key != 'UNTIL' else val}")
    normalised = ";".join(out)
    try:
        rrulestr(normalised, dtstart=datetime(2000, 1, 1))
    except (ValueError, TypeError) as err:
        raise Invalid(f"rrule could not be read: {err}.", hint=RULE_HINT) from None
    return normalised


def _local_until(value: str, tz: ZoneInfo) -> str:
    """UNTIL as local wall-clock time. A bare date means the end of that day, so
    "until 31 December" includes 31 December's slot."""
    value = value.strip().upper()
    if re.fullmatch(r"\d{8}", value):
        return value + "T235959"
    match = _UNTIL_Z.match(value)
    if not match:
        return value
    instant = datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    return instant.astimezone(tz).strftime("%Y%m%dT%H%M%S")


def dtstart_for(now: datetime, tz: ZoneInfo, due: time) -> datetime:
    """Today's local date at the due time, naive (SPEC 7.1)."""
    return datetime.combine(local_date(now, tz), due)


def _rule(task: Task) -> rrule:
    return rrulestr(task.rrule, dtstart=task.recur_dtstart)


def next_slot(task: Task, after: datetime, tz: ZoneInfo) -> datetime | None:
    """The first slot strictly later than `after`, as aware UTC."""
    local_after = after.astimezone(tz).replace(tzinfo=None)
    found = _rule(task).after(local_after, inc=False)
    while found is not None:
        slot = to_utc(found, tz)
        if slot > after:  # a fall-back hour can map a later wall time to an earlier instant
            return slot
        found = _rule(task).after(found, inc=False)
    return None


def next_slots(
    task: Task, after: datetime, tz: ZoneInfo, count: int = NEXT_SLOTS
) -> list[datetime]:
    out: list[datetime] = []
    while len(out) < count:
        slot = next_slot(task, out[-1] if out else after, tz)
        if slot is None:
            break
        out.append(slot)
    return out


# --- Instances ---


def open_instances(session: Session, recurring: Task) -> list[Task]:
    """Open instances, oldest occurrence first (then by id)."""
    return list(
        session.scalars(
            sa.select(Task)
            .where(Task.parent_id == recurring.id, Task.status == Status.OPEN)
            .order_by(Task.occurrence_at.is_(None), Task.occurrence_at, Task.id)
        )
    )


def make_instance(
    session: Session, recurring: Task, now: datetime, *, slot: datetime | None, triaged: bool
) -> Task:
    """A new instance: the title and parent, plus the slot and its deadline for
    a scheduled task. Nothing else is copied (SPEC 7.1)."""
    task = Task(
        title=recurring.title,
        parent_id=recurring.id,
        status=Status.OPEN,
        triaged=triaged,
        achievable=Achievable.UNKNOWN,
        conditions=[],
        field_sources={},
        occurrence_at=slot,
        created_at=now,
        updated_at=now,
    )
    if slot is not None:
        task.deadline = slot
        task.deadline_firmness = recurring.recur_firmness
        # The user's own schedule set this deadline, so triage guesses cannot move it.
        task.field_sources["deadline"] = FieldSource.USER.value
    session.add(task)
    session.flush()
    return task


def _drop_open_descendants(session: Session, task: Task, now: datetime, reason: str) -> None:
    stack = [task.id]
    while stack:
        parent_id = stack.pop()
        children = session.scalars(
            sa.select(Task)
            .where(Task.parent_id == parent_id, Task.status == Status.OPEN)
            .order_by(Task.id)
        )
        for child in children:
            child.status, child.closed_at, child.updated_at = Status.DROPPED, now, now
            record(
                session,
                EventType.DROPPED,
                at=now,
                task_id=child.id,
                payload={"reason": reason, "cascade_from": task.id},
            )
            stack.append(child.id)


def resume(session: Session, recurring: Task, now: datetime, ended: datetime, tz: ZoneInfo) -> None:
    """End a pause: skip the paused slots, never moving backward (SPEC 7.1)."""
    recurring.last_spawned_slot = max(recurring.last_spawned_slot, ended)
    recurring.snooze_until = None
    recurring.updated_at = now
    record(
        session,
        EventType.RESUMED,
        at=now,
        task_id=recurring.id,
        payload={"skipped_until": at_text(ended, tz)},
    )


def roll_forward(session: Session, now: datetime, tz: ZoneInfo) -> None:
    """Apply everything the clock has made due. Runs at the start of every request."""
    scheduled = session.scalars(
        sa.select(Task)
        .where(Task.status == Status.OPEN, Task.recur_mode == RecurMode.SCHEDULED)
        .order_by(Task.id)
    ).all()
    for recurring in scheduled:
        if recurring.snooze_until is not None:
            if recurring.snooze_until > now:
                continue  # paused
            resume(session, recurring, now, recurring.snooze_until, tz)
        lead = timedelta(minutes=recurring.lead_minutes)
        spawned = 0
        slot = next_slot(recurring, recurring.last_spawned_slot, tz)
        while slot is not None and slot - lead <= now and spawned < SPAWN_LIMIT:
            live = open_instances(session, recurring)
            oldest = live[0] if len(live) >= INSTANCE_CAP else None
            if oldest is not None:
                _drop_open_descendants(session, oldest, now, "instance missed")
                oldest.status, oldest.closed_at, oldest.updated_at = Status.MISSED, now, now
            instance = make_instance(session, recurring, now, slot=slot, triaged=False)
            if oldest is not None:
                record(
                    session,
                    EventType.MISSED,
                    at=now,
                    task_id=oldest.id,
                    payload={"recurring_id": recurring.id, "replaced_by": instance.id},
                )
            record(
                session,
                EventType.SPAWNED,
                at=now,
                task_id=instance.id,
                payload={"recurring_id": recurring.id, "occurrence_at": at_text(slot, tz)},
            )
            recurring.last_spawned_slot = slot
            slot = next_slot(recurring, slot, tz)
            spawned += 1
        if slot is None and not open_instances(session, recurring):
            recurring.status, recurring.closed_at, recurring.updated_at = Status.DROPPED, now, now
            record(
                session,
                EventType.DROPPED,
                at=now,
                task_id=recurring.id,
                payload={"reason": "schedule ended"},
            )
    session.flush()


# --- The recurrence object (SPEC 8.2.1) ---

SCHEDULED_KEYS = frozenset({"mode", "cadence", "rrule", "due_time", "lead_days", "firmness"})
FLEXIBLE_KEYS = frozenset({"mode", "cadence", "gap_days"})
_DUE_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass
class Recurrence:
    """A parsed recurrence object: only the keys that were given."""

    mode: RecurMode
    given: dict[str, Any]


def parse_recurrence(value: Any, tz: ZoneInfo, *, mode: RecurMode | None = None) -> Recurrence:
    """Read a recurrence object. `mode` is the task's current mode when changing
    an existing recurring task, where a partial object is allowed."""
    if not isinstance(value, dict):
        raise Invalid(
            "recurrence must be an object.",
            hint='For example {"mode": "flexible", "cadence": "about every five days", '
            '"gap_days": 5}.',
        )
    given = dict(value)
    stated = given.pop("mode", None)
    if stated is not None:
        stated = wire.parse_enum(RecurMode, stated, "recurrence.mode")
        if mode is not None and stated is not mode:
            raise Invalid(
                f"This task is {mode.value}; its mode cannot change.",
                hint="Drop it and create a new recurring task with the other mode.",
            )
    chosen = mode or stated
    if chosen is None:
        raise Invalid(
            "recurrence needs a mode: scheduled or flexible.",
            hint='"scheduled" for a calendar rule such as every Friday; "flexible" for '
            "everything else.",
        )
    allowed = SCHEDULED_KEYS if chosen is RecurMode.SCHEDULED else FLEXIBLE_KEYS
    wrong = sorted(set(given) - allowed)
    if wrong:
        raise Invalid(
            f"recurrence keys {', '.join(wrong)} do not apply to a {chosen.value} task.",
            hint=f"A {chosen.value} recurrence takes: {', '.join(sorted(allowed))}.",
        )
    out: dict[str, Any] = {}
    if "cadence" in given:
        cadence = given["cadence"]
        if cadence is not None and not isinstance(cadence, str):
            raise Invalid("recurrence.cadence must be text.", hint='e.g. "every Friday by 5pm"')
        out["cadence"] = (cadence or "").strip() or None
    if "rrule" in given:
        out["rrule"] = normalise_rrule(given["rrule"], tz)
    if "due_time" in given:
        match = _DUE_TIME.match(str(given["due_time"]).strip())
        if not match:
            raise Invalid("recurrence.due_time must be HH:MM.", hint='e.g. "17:00".')
        out["due_time"] = time(int(match[1]), int(match[2]))
    if "lead_days" in given:
        lead = given["lead_days"]
        if isinstance(lead, bool) or not isinstance(lead, int | float) or not 0 <= lead <= 365:
            raise Invalid(
                "recurrence.lead_days must be a number from 0 to 365.",
                hint="How many days before each slot it should appear, e.g. 1 or 0.5.",
            )
        out["lead_minutes"] = math.floor(lead * 24 * 60)
    if "firmness" in given:
        out["recur_firmness"] = wire.parse_enum(Firmness, given["firmness"], "recurrence.firmness")
    if "gap_days" in given:
        gap = given["gap_days"]
        if isinstance(gap, bool) or not isinstance(gap, int) or gap < 1:
            raise Invalid(
                "recurrence.gap_days must be a whole number, 1 or more.",
                hint="Days to wait after each completion before it comes back, e.g. 5.",
            )
        out["gap_days"] = gap
    return Recurrence(chosen, out)


def describe(task: Task, tz: ZoneInfo) -> dict[str, Any]:
    """A recurring task's recurrence object, for the event log."""
    out: dict[str, Any] = {"mode": task.recur_mode.value, "cadence": task.cadence}
    if task.recur_mode is RecurMode.SCHEDULED:
        out.update(
            rrule=task.rrule,
            due_time=task.recur_dtstart.strftime("%H:%M"),
            lead_days=round(task.lead_minutes / (24 * 60), 3),
            firmness=task.recur_firmness.value,
        )
    else:
        out["gap_days"] = task.gap_days
    return {k: v for k, v in out.items() if v is not None}


def apply(task: Task, parsed: Recurrence, now: datetime, tz: ZoneInfo, *, creating: bool) -> None:
    """Write a parsed recurrence onto a task: a new recurring task (or a converted
    plain one) when `creating`, otherwise a change to an existing one."""
    g = parsed.given
    if creating:
        task.recur_mode = parsed.mode
        if parsed.mode is RecurMode.SCHEDULED:
            if "rrule" not in g:
                raise Invalid("A scheduled recurrence needs an rrule.", hint=RULE_HINT)
            task.lead_minutes = g.get("lead_minutes", DEFAULT_LEAD_DAYS * 24 * 60)
            task.recur_firmness = g.get("recur_firmness", Firmness.HARD)
            task.last_spawned_slot = now
    if "cadence" in g:
        task.cadence = g["cadence"]
    if "gap_days" in g:
        task.gap_days = g["gap_days"]
    if parsed.mode is RecurMode.SCHEDULED:
        if "lead_minutes" in g:
            task.lead_minutes = g["lead_minutes"]
        if "recur_firmness" in g:
            task.recur_firmness = g["recur_firmness"]
        if creating or "rrule" in g or "due_time" in g:
            due = g.get("due_time") or (DEFAULT_DUE_TIME if creating else task.recur_dtstart.time())
            task.rrule = g.get("rrule", task.rrule)
            task.recur_dtstart = dtstart_for(now, tz, due)
            task.last_spawned_slot = max(task.last_spawned_slot or now, now)
            if next_slot(task, now, tz) is None:
                raise Invalid(
                    "That rule has no dates after now.",
                    hint="Check COUNT and UNTIL, or leave them out. " + RULE_HINT,
                )
