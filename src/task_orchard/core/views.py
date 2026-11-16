"""Builders for the JSON the tools return.

SPEC section 4. Three shapes and nothing else:

- task_row: the compact row used in every list (4.3);
- task_detail: one task in full, for get_task and write responses (4.4);
- respond: the wrapper every response shares (`now`, plus the payload).

Keys whose value is null, empty, false or zero are omitted, to save context,
except the ones a caller names as always present.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from task_orchard.core import history, recurrence, timefmt
from task_orchard.core.enums import (
    TRACKED_FIELDS,
    Achievable,
    EventType,
    Firmness,
    RecurMode,
    Status,
)
from task_orchard.db.models import Event, Pattern, Task

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from task_orchard.core.derive import Effective, Snapshot

ROW_TITLE_CHARS = 80
NESTED_TITLE_CHARS = 40
PATH_DEPTH = 3
ROW_BLOCKERS = 3
DETAIL_CHILDREN = 20
DETAIL_EVENTS = 10
DETAIL_NOTES_CHARS = 2000
EVENT_NOTE_CHARS = 120
PATTERN_HINT_CHARS = 120
PATTERN_DESCRIPTION_MAX_CHARS = 600
TOUCHED_AFTER = timedelta(days=1)
ELLIPSIS = "…"


def compact(data: dict[str, Any], *, keep: Iterable[str] = ()) -> dict[str, Any]:
    """Drop keys whose value is None, empty, False or 0, except those in `keep`."""
    kept = set(keep)
    return {
        k: v
        for k, v in data.items()
        if k in kept or not (v is None or v is False or v == 0 or v == [] or v == {})
    }


def truncate(text: str, limit: int) -> str:
    """Cut to `limit` characters, the trailing ellipsis included."""
    return text if len(text) <= limit else text[: limit - 1] + ELLIPSIS


def ref(task: Task) -> dict[str, Any]:
    """A nested reference: {id, title} with the short title limit."""
    return {"id": task.id, "title": truncate(task.title, NESTED_TITLE_CHARS)}


def respond(now: datetime, tz: ZoneInfo, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """Every response: the payload plus `now`."""
    return {**compact(payload, **kw), "now": timefmt.now_stamp(now, tz)}


def _label(value: Any) -> Any:
    if value is None:
        return None
    return getattr(value, "label", None) or getattr(value, "value", value)


def wire_name(value: Any) -> Any:
    """An enum member as its wire name; anything else unchanged."""
    return _label(value)


# --- Wire forms of single fields, shared with the event log ---


def wire_value(field: str, task: Task, tz: ZoneInfo) -> Any:
    """A task's own value for `field`, in the form a caller reads and writes."""
    if field == "deadline":
        if task.deadline is None:
            return None
        return {"at": timefmt.at_text(task.deadline, tz), "firmness": task.deadline_firmness.value}
    if field == "conditions":
        return list(task.conditions or []) or None
    if field == "snooze_until":
        return None if task.snooze_until is None else timefmt.at_text(task.snooze_until, tz)
    value = getattr(task, field)
    if field == "achievable" and value is Achievable.UNKNOWN:
        return None
    return _label(value)


# --- The row (4.3) ---


def path(snap: Snapshot, task: Task) -> list[str]:
    """Ancestor titles, root first, cut to PATH_DEPTH (SPEC 4.3)."""
    above = snap.ancestors(task)
    names = [truncate(t.title, NESTED_TITLE_CHARS) for t in reversed(above[:PATH_DEPTH])]
    if len(above) > PATH_DEPTH:
        names.insert(0, ELLIPSIS)
    return names


def _via(snap: Snapshot, found: Effective) -> dict[str, Any] | None:
    if found.via is None:
        return None
    return {**ref(snap.task(found.via)), "how": found.how}


def _deadline(snap: Snapshot, found: Effective | None, tz: ZoneInfo) -> dict[str, Any] | None:
    if found is None:
        return None
    return compact(
        {
            **timefmt.stamp(found.value, tz, snap.now, deadline=True),
            "firmness": found.firmness.value,
            "via": _via(snap, found),
        }
    )


def task_row(
    snap: Snapshot,
    task: Task,
    tz: ZoneInfo,
    *,
    always_status: bool = False,
    blockers: int = ROW_BLOCKERS,
    blocker_via: bool = False,
) -> dict[str, Any]:
    """The row of SPEC 4.3. `blockers` and `blocker_via` widen `blocked_by`
    for the critical list (5.8)."""
    now = snap.now
    row: dict[str, Any] = {"id": task.id, "title": truncate(task.title, ROW_TITLE_CHARS)}
    if always_status or task.status is not Status.OPEN:
        row["status"] = task.status.value
    if snap.is_instance(task):
        row["instance_of"] = task.parent_id
    row["path"] = path(snap, task)

    priority = snap.effective_priority(task)
    if priority is not None:
        row["priority"] = _label(priority.value)
        row["priority_via"] = _via(snap, priority)
        if task.priority is not priority.value:
            row["priority_own"] = _label(task.priority)
    due = snap.effective_due(task)
    row["due"] = _deadline(snap, due, tz)
    if due is not None and due.firmness is not Firmness.HARD:
        hard_due = snap.effective_due(task, hard=True)
        if hard_due is not None and hard_due.value > due.value:
            row["hard_due"] = _deadline(snap, hard_due, tz)
    row["estimate_minutes"] = task.estimate_minutes
    row["activation"] = _label(task.activation)
    row["drain"] = _label(task.drain)
    motive = snap.effective_motive(task)
    row["motive"] = None if motive is None else _label(motive.value)
    if not task.is_recurring and task.achievable is not Achievable.YES:
        row["achievable"] = task.achievable.value
    row["state"] = snap.state(task)
    if task.status is Status.OPEN and snap.dormant_by_own_snooze(task):
        row["snoozed_until"] = timefmt.stamp(task.snooze_until, tz, now)
    if task.status is Status.OPEN:
        found = snap.effective_blockers(task)
        row["blocked_by"] = [
            compact({**ref(snap.task(b)), "via": ref(snap.task(v)) if blocker_via and v else None})
            for b, v in found[:blockers]
        ]
        row["blocked_by_more"] = max(0, len(found) - blockers)
    row["unblocks"] = snap.unblocks(task)
    row["conditions"] = list(task.conditions or [])
    counts = snap.counts(task)
    if not task.is_recurring:
        last = snap.last_touched(task)
        if now - last > TOUCHED_AFTER:
            row["touched"] = timefmt.relative(last, now)
    row["deferred"] = counts.deferred
    row["starts"] = counts.starts
    if task.is_recurring:
        row["recurring"] = recurring_block(snap, task, tz)
    if task.pattern_id is not None:
        found = snap.pattern(task.pattern_id)
        row["pattern"] = compact(
            {
                "id": found.id,
                "name": truncate(found.name, NESTED_TITLE_CHARS),
                "hint": truncate(found.learned, PATTERN_HINT_CHARS) if found.learned else None,
            }
        )
    keep = ("id", "title", "status") if always_status else ("id", "title")
    return compact(row, keep=keep)


# --- Recurring tasks (7) ---


def recurring_block(snap: Snapshot, task: Task, tz: ZoneInfo) -> dict[str, Any]:
    """The `recurring` key of a recurring task's row (SPEC 4.3, 7.2)."""
    now = snap.now
    done, last = snap.recurring_done(task)
    block: dict[str, Any] = {
        "mode": task.recur_mode.value,
        "cadence": task.cadence,
        "gap_days": task.gap_days,
        "last_done": None if last is None else timefmt.relative(last, now),
        "done_count": done,
    }
    if task.recur_mode is RecurMode.FLEXIBLE:
        if task.gap_days and task.snooze_until is not None and task.snooze_until <= now:
            block["past_wake_days"] = (now - task.snooze_until).days
    elif task.status is Status.OPEN:
        after = task.last_spawned_slot
        if task.snooze_until is not None and task.snooze_until > after:
            after = task.snooze_until  # paused: the slots before it are skipped
        upcoming = recurrence.next_slot(task, after, tz)
        block["next_slot"] = None if upcoming is None else timefmt.stamp(upcoming, tz, now)
    return compact(block)


# --- Pattern digests (6.1) ---


def pattern_digests(
    session: Any, patterns: list[Pattern], tz: ZoneInfo, now: datetime
) -> list[dict[str, Any]]:
    out = []
    for digest in history.pattern_digests(session, patterns):
        last = digest["last_done"]
        digest["last_done"] = None if last is None else timefmt.stamp(last, tz, now)
        if digest["description"]:  # stored before the limit existed
            digest["description"] = truncate(digest["description"], PATTERN_DESCRIPTION_MAX_CHARS)
        out.append(compact(digest, keep=("id", "name")))
    return out


def pattern_digest(session: Any, pattern: Pattern, tz: ZoneInfo, now: datetime) -> dict[str, Any]:
    return pattern_digests(session, [pattern], tz, now)[0]


# --- The detail (4.4) ---


def _fields(task: Task, tz: ZoneInfo) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in TRACKED_FIELDS:
        if field == "achievable" and task.is_recurring:
            continue
        value = wire_value(field, task, tz)
        source = (task.field_sources or {}).get(field)
        if value is None and not (field == "achievable" and source):
            continue
        if field == "achievable" and value is None:
            value = Achievable.UNKNOWN.value
        if field == "deadline":
            out[field] = {"value": value["at"], "firmness": value["firmness"], "source": source}
        else:
            out[field] = {"value": value, "source": source}
        out[field] = compact(out[field])
    return out


def _completion(task: Task, snap: Snapshot, tz: ZoneInfo) -> dict[str, Any] | None:
    done = task.completion
    if done is None or task.status is not Status.DONE:
        return None
    return compact(
        {
            "completed": timefmt.stamp(done.completed_at, tz, snap.now),
            "actual_minutes": done.actual_minutes,
            "felt_activation": _label(done.felt_activation),
            "felt_drain": _label(done.felt_drain),
            "reflection": done.reflection,
        }
    )


def _brief(value: Any) -> str:
    if isinstance(value, dict) and "at" in value:
        return " ".join(str(v) for v in (value["at"], value.get("firmness")) if v)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "none"
    return "none" if value is None else str(value)


def event_note(event: Event) -> str:
    """A one-line summary of an event's payload, for recent_events."""
    p = event.payload or {}
    if event.type is EventType.CREATED:
        text = f"via {p.get('via', '?')}"
        if p.get("parent_id"):
            text += f" under #{p['parent_id']}"
    elif event.type is EventType.UPDATED:
        parts = []
        for field, change in (p.get("changes") or {}).items():
            part = f"{field} → {_brief(change['to'])}" if "to" in change else field
            if change.get("source"):
                part += f" ({change['source']})"
            parts.append(part)
        text = "; ".join(parts)
    elif event.type in (EventType.LINKED, EventType.UNLINKED):
        verb = "blocked by" if event.type is EventType.LINKED else "no longer blocked by"
        text = f"{verb} #{p.get('blocker_id')}"
    elif event.type is EventType.REOPENED:
        text = f"was {p.get('was', '?')}"
    elif event.type is EventType.CONFLICT:
        text = (
            f"kept {p.get('field')} = {_brief(p.get('kept'))}; "
            f"{p.get('source')} suggested {_brief(p.get('proposed'))}"
        )
    else:
        text = ", ".join(f"{k} {_brief(v)}" for k, v in p.items() if v not in (None, [], {}))
    return truncate(text, EVENT_NOTE_CHARS)


def _recent_events(snap: Snapshot, task: Task) -> list[dict[str, Any]]:
    events = snap.session.scalars(
        sa.select(Event)
        .where(Event.task_id == task.id)
        .order_by(Event.at.desc(), Event.id.desc())
        .limit(DETAIL_EVENTS)
    )
    return [
        compact(
            {
                "type": e.type.value,
                "when": timefmt.relative(e.at, snap.now),
                "note": event_note(e),
            }
        )
        for e in events
    ]


def task_detail(snap: Snapshot, task: Task, tz: ZoneInfo) -> dict[str, Any]:
    now = snap.now
    detail = task_row(snap, task, tz)
    detail.pop("blocked_by", None)
    detail.pop("blocked_by_more", None)

    notes = task.notes or ""
    extra: dict[str, Any] = {
        "description": task.description,
        "notes": truncate(notes, DETAIL_NOTES_CHARS) if notes else None,
        "notes_truncated": len(notes) > DETAIL_NOTES_CHARS,
        "fields": _fields(task, tz),
    }
    if task.parent_id is not None:
        extra["parent"] = ref(snap.task(task.parent_id))

    children = list(task.children)
    open_children = [c for c in children if c.status is Status.OPEN]
    extra["children"] = [
        compact({"id": c.id, "title": truncate(c.title, ROW_TITLE_CHARS), "state": snap.state(c)})
        for c in open_children[:DETAIL_CHILDREN]
    ]
    extra["children_open"] = len(open_children)
    extra["children_closed"] = len(children) - len(open_children)

    extra["blocks"] = [
        {"id": b.id, "title": truncate(b.title, ROW_TITLE_CHARS), "status": b.status.value}
        for b in (snap.task(i) for i in snap.blocking.get(task.id, []))
    ]
    if task.status is Status.OPEN:
        extra["blocked_by"] = [
            compact({**ref(snap.task(b)), "via": ref(snap.task(via)) if via else None})
            for b, via in snap.effective_blockers(task)
        ]
    extra["completion"] = _completion(task, snap, tz)
    extra["recent_events"] = _recent_events(snap, task)
    extra["created"] = timefmt.stamp(task.created_at, tz, now)
    if task.closed_at is not None:
        extra["closed"] = timefmt.stamp(task.closed_at, tz, now)
    if task.pattern_id is not None:
        extra["pattern"] = pattern_digest(snap.session, snap.pattern(task.pattern_id), tz, snap.now)

    return compact({**detail, **extra}, keep=("id", "title"))
