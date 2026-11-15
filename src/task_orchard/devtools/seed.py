"""Demo data: a small, realistic orchard covering every shape the spec describes.

Used by `task-orchard seed-demo` (for trying skills by hand in an MCP client)
and by tests that want a populated database. Every date is relative to `now`,
so the demo never goes stale.

This writes rows directly instead of going through the service, because it
exists before the service does. Once Phase 3 is done it may be rewritten on top
of the service, as long as the handles below keep their meaning; tests refer to
tasks by handle.

What is in it, by handle:

  Trees
    observability        project, not achievable, soft deadline in 21 days
      obs_backend          done, took 150 min against a 60 min estimate
      obs_spec             open, achievable; pattern "Interface specs"
      obs_collector        open, blocked by obs_spec
    release              project, critical, HARD deadline in 20 hours
      release_flaky        open, achievable
      release_notes        open, blocked by release_flaky
      release_publish      open, blocked by release_notes and by `cert`
    cert                 root task, LOW priority, no deadline, blocks release_publish
                         (so it should come out critical, with a hard deadline
                         20 minutes before the release's: publish's estimate)
    game                 project, motive want
      game_movement        open, high drain
      game_build           open
    budget               root task, blocks `offsite`
    offsite              project
      offsite_venue        open; blocked only by inheritance, through `offsite`
      offsite_invite       open, blocked by offsite_venue

  Single tasks
    dentist              high activation, low drain; snoozed 4 times, started twice
    mechanic             plain achievable task
    taxes                hard deadline in 40 days, snoozed for 20 (dormant)
    novel                motive want, no deadline
    milk                 soft deadline 6 days ago
    migration_spec       done 30 days ago; pattern "Interface specs"

  Inbox (untriaged)
    inbox_desk, inbox_reply, inbox_run

  Recurring
    summary              scheduled, every Friday 17:00, hard, lead 2 days
      summary_w0           done on time
      summary_w1           missed (closed by the two-instance cap)
      summary_w2           done six days late
      summary_w3           open and overdue
    laundry              flexible, gap 5 days, last done 6 days ago (awake)
    run                  flexible, gap 2 days, done yesterday (dormant)
    docs                 flexible, no gap, "when there's free time"
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session

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
    TaskKind,
)
from task_orchard.core.events import record
from task_orchard.db.models import Completion, Pattern, ProfileNote, Task, TaskBlock

LEAD = timedelta(days=2)


class DatabaseNotEmpty(RuntimeError):
    pass


def _friday_slots(now: datetime, tz: ZoneInfo, count: int) -> list[datetime]:
    """The last `count` Fridays at 17:00 local that are not after `now`, oldest first."""
    local = now.astimezone(tz)
    day = local.replace(hour=17, minute=0, second=0, microsecond=0)
    while day.weekday() != 4 or day > local:
        day -= timedelta(days=1)
        day = day.replace(hour=17, minute=0)
    slots = []
    for weeks_back in range(count - 1, -1, -1):
        wall = (day - timedelta(weeks=weeks_back)).replace(tzinfo=None)
        slots.append(wall.replace(tzinfo=tz).astimezone(now.tzinfo))
    return slots


class _Seeder:
    def __init__(self, session: Session, now: datetime, tz: ZoneInfo) -> None:
        self.s = session
        self.now = now
        self.tz = tz
        self.handles: dict[str, int] = {}

    def ago(self, **kwargs: float) -> datetime:
        return self.now - timedelta(**kwargs)

    def ahead(self, **kwargs: float) -> datetime:
        return self.now + timedelta(**kwargs)

    def pattern(self, name: str, learned: str | None = None, description: str | None = None):
        p = Pattern(
            name=name,
            learned=learned,
            description=description,
            created_at=self.ago(days=60),
            updated_at=self.ago(days=3),
        )
        self.s.add(p)
        self.s.flush()
        return p

    def task(
        self,
        handle: str,
        title: str,
        *,
        created: datetime | None = None,
        parent: Task | None = None,
        triaged: bool = True,
        sources: dict[str, FieldSource] | None = None,
        **fields: Any,
    ) -> Task:
        created = created or self.ago(days=14)
        if fields.get("deadline") is not None:
            fields.setdefault("deadline_firmness", Firmness.SOFT)
        task = Task(
            title=title,
            parent_id=parent.id if parent else None,
            triaged=triaged,
            created_at=created,
            updated_at=created,
            field_sources={k: v.value for k, v in (sources or {}).items()},
            **fields,
        )
        self.s.add(task)
        self.s.flush()
        self.handles[handle] = task.id
        record(self.s, EventType.CREATED, at=created, task_id=task.id, payload={"via": "seed"})
        return task

    def block(self, blocker: Task, blocked: Task) -> None:
        at = max(blocker.created_at, blocked.created_at)
        self.s.add(TaskBlock(blocker_id=blocker.id, blocked_id=blocked.id, created_at=at))
        record(
            self.s,
            EventType.LINKED,
            at=at,
            task_id=blocked.id,
            payload={"blocker_id": blocker.id, "blocked_id": blocked.id},
        )

    def complete(self, task: Task, at: datetime, motive: Motive | None, **fields: Any) -> None:
        task.status = Status.DONE
        task.closed_at = at
        task.updated_at = at
        self.s.add(
            Completion(
                task_id=task.id,
                completed_at=at,
                estimate_at_completion=task.estimate_minutes,
                motive_at_completion=motive,
                **fields,
            )
        )
        record(self.s, EventType.COMPLETED, at=at, task_id=task.id, payload={"via": "seed"})


def seed_demo(session: Session, now: datetime, tz: ZoneInfo) -> dict[str, int]:
    """Fill an empty database. Returns {handle: task id}."""
    if session.scalar(sa.select(sa.func.count()).select_from(Task)):
        raise DatabaseNotEmpty("the database already has tasks")
    z = _Seeder(session, now, tz)
    user, guess, pattern = FieldSource.USER, FieldSource.GUESS, FieldSource.PATTERN

    # --- Patterns and profile ---
    calls = z.pattern(
        "Phone calls to book things",
        learned="Hard to start, five minutes once dialled. Goes better straight after lunch.",
    )
    specs = z.pattern(
        "Interface specs",
        learned="Takes about twice the estimate. Needs an uninterrupted morning.",
    )
    for topic, text in [
        (
            ProfileNote.CHARTER_TOPIC,
            "Help me keep work obligations from eating everything else. I want at least a "
            "few hours a week on my game. Be direct, give me two or three options, and "
            "don't nag about chores.",
        ),
        ("energy", "Energy is low after 3pm on weekdays. Mornings are for hard thinking."),
        ("chores", "No chores before noon."),
    ]:
        session.add(
            ProfileNote(topic=topic, text=text, created_at=z.ago(days=40), updated_at=z.ago(days=9))
        )

    # --- Observability: a project with a defined leading edge ---
    obs = z.task(
        "observability",
        "Design, build and deploy an observability stack for my application",
        created=z.ago(days=20),
        priority=Priority.HIGH,
        motive=Motive.HAVE_TO,
        achievable=Achievable.NO,
        deadline=z.ahead(days=21),
        sources={"priority": user, "deadline": user, "motive": guess},
    )
    backend = z.task(
        "obs_backend",
        "Pick a metrics backend",
        created=z.ago(days=20),
        parent=obs,
        achievable=Achievable.YES,
        estimate_minutes=60,
        drain=Level.MEDIUM,
        activation=Level.LOW,
    )
    z.complete(
        backend,
        z.ago(days=3),
        Motive.HAVE_TO,
        actual_minutes=150,
        felt_drain=Level.HIGH,
        felt_activation=Level.LOW,
        reflection="Took far longer than expected: too many options and no clear criteria.",
    )
    spec = z.task(
        "obs_spec",
        "Write the new interface spec",
        created=z.ago(days=20),
        parent=obs,
        achievable=Achievable.YES,
        estimate_minutes=120,
        drain=Level.HIGH,
        activation=Level.MEDIUM,
        conditions=["uninterrupted"],
        pattern_id=specs.id,
        sources={"estimate_minutes": pattern, "drain": pattern},
    )
    collector = z.task(
        "obs_collector",
        "Stand up a staging collector",
        created=z.ago(days=20),
        parent=obs,
    )
    z.block(spec, collector)

    # --- Release: a hard deadline that reaches a low-priority root task ---
    release = z.task(
        "release",
        "Ship release 2.4",
        created=z.ago(days=10),
        priority=Priority.CRITICAL,
        motive=Motive.HAVE_TO,
        achievable=Achievable.NO,
        deadline=z.ahead(hours=20),
        deadline_firmness=Firmness.HARD,
        sources={"priority": user, "deadline": user},
    )
    flaky = z.task(
        "release_flaky",
        "Fix the flaky login test",
        created=z.ago(days=10),
        parent=release,
        achievable=Achievable.YES,
        estimate_minutes=90,
        drain=Level.MEDIUM,
    )
    notes = z.task(
        "release_notes",
        "Write the release notes",
        created=z.ago(days=10),
        parent=release,
        achievable=Achievable.YES,
        estimate_minutes=45,
        drain=Level.LOW,
    )
    z.block(flaky, notes)
    publish = z.task(
        "release_publish",
        "Publish the signed build",
        created=z.ago(days=10),
        parent=release,
        achievable=Achievable.YES,
        estimate_minutes=20,
        drain=Level.LOW,
    )
    z.block(notes, publish)
    cert = z.task(
        "cert",
        "Renew the signing certificate",
        created=z.ago(days=25),
        priority=Priority.LOW,
        motive=Motive.HAVE_TO,
        achievable=Achievable.YES,
        estimate_minutes=30,
        activation=Level.MEDIUM,
        drain=Level.LOW,
    )
    # On the one step that needs it, not on the project: a blocker on a parent
    # would block every child, including work that does not need the certificate.
    z.block(cert, publish)

    # --- The game: wanted, hard to start, easily crowded out ---
    game = z.task(
        "game",
        "Make my game",
        created=z.ago(days=45),
        priority=Priority.NORMAL,
        motive=Motive.WANT,
        achievable=Achievable.NO,
        sources={"motive": user},
    )
    z.task(
        "game_movement",
        "Prototype the movement system",
        created=z.ago(days=30),
        parent=game,
        achievable=Achievable.YES,
        estimate_minutes=180,
        activation=Level.MEDIUM,
        drain=Level.HIGH,
    )
    z.task(
        "game_build",
        "Fix the build pipeline",
        created=z.ago(days=30),
        parent=game,
        achievable=Achievable.YES,
        estimate_minutes=60,
        activation=Level.MEDIUM,
        drain=Level.MEDIUM,
    )

    # --- Offsite: blocking inherited through a parent ---
    budget = z.task(
        "budget",
        "Get the offsite budget approved",
        created=z.ago(days=8),
        motive=Motive.HAVE_TO,
        achievable=Achievable.YES,
        estimate_minutes=30,
    )
    offsite = z.task(
        "offsite",
        "Plan the team offsite",
        created=z.ago(days=8),
        motive=Motive.HAVE_TO,
        achievable=Achievable.NO,
    )
    z.block(budget, offsite)
    venue = z.task(
        "offsite_venue",
        "Book a venue",
        created=z.ago(days=8),
        parent=offsite,
        achievable=Achievable.YES,
        estimate_minutes=45,
    )
    invite = z.task(
        "offsite_invite",
        "Send the invitation",
        created=z.ago(days=8),
        parent=offsite,
        achievable=Achievable.YES,
        estimate_minutes=20,
    )
    z.block(venue, invite)

    # --- Single tasks ---
    dentist = z.task(
        "dentist",
        "Call the dentist to book a check-up",
        created=z.ago(days=35),
        priority=Priority.NORMAL,
        motive=Motive.HAVE_TO,
        achievable=Achievable.YES,
        estimate_minutes=10,
        activation=Level.HIGH,
        drain=Level.LOW,
        conditions=["business-hours"],
        pattern_id=calls.id,
        sources={"activation": FieldSource.EVIDENCE, "estimate_minutes": pattern},
    )
    for days in (30, 22, 15, 6):
        record(
            session,
            EventType.SNOOZED,
            at=z.ago(days=days),
            task_id=dentist.id,
            payload={"until": z.ago(days=days - 5).isoformat(), "reason": "not today"},
        )
    for days in (16, 7):
        record(session, EventType.STARTED, at=z.ago(days=days), task_id=dentist.id)
    z.task(
        "mechanic",
        "Call the mechanic",
        created=z.ago(days=2),
        motive=Motive.HAVE_TO,
        achievable=Achievable.YES,
        estimate_minutes=10,
        activation=Level.MEDIUM,
        drain=Level.LOW,
        conditions=["business-hours"],
        pattern_id=calls.id,
    )
    z.task(
        "taxes",
        "File taxes",
        created=z.ago(days=50),
        priority=Priority.HIGH,
        motive=Motive.HAVE_TO,
        achievable=Achievable.NO,
        deadline=z.ahead(days=40),
        deadline_firmness=Firmness.HARD,
        snooze_until=z.ahead(days=20),
        sources={"deadline": user},
    )
    z.task(
        "novel",
        "Read 'The Dispossessed'",
        created=z.ago(days=60),
        priority=Priority.LOW,
        motive=Motive.WANT,
        achievable=Achievable.YES,
        activation=Level.LOW,
        drain=Level.LOW,
    )
    z.task(
        "milk",
        "Buy milk",
        created=z.ago(days=8),
        priority=Priority.LOW,
        motive=Motive.HAVE_TO,
        achievable=Achievable.YES,
        estimate_minutes=15,
        deadline=z.ago(days=6),
    )
    migration = z.task(
        "migration_spec",
        "Write the API migration spec",
        created=z.ago(days=45),
        motive=Motive.HAVE_TO,
        achievable=Achievable.YES,
        estimate_minutes=120,
        drain=Level.HIGH,
        pattern_id=specs.id,
    )
    z.complete(
        migration,
        z.ago(days=30),
        Motive.HAVE_TO,
        actual_minutes=260,
        felt_drain=Level.HIGH,
        felt_activation=Level.MEDIUM,
        reflection="Kept getting interrupted. Finished it in one go on a quiet morning.",
    )

    # --- Inbox ---
    z.task("inbox_desk", "look into a standing desk", created=z.ago(days=4), triaged=False)
    z.task("inbox_reply", "reply to Priya about the workshop", created=z.ago(days=1), triaged=False)
    z.task("inbox_run", "I want to go running twice a week", created=z.ago(hours=3), triaged=False)

    # --- Scheduled recurrence: every Friday at 17:00 ---
    slots = _friday_slots(now, tz, 4)
    summary_pattern = z.pattern("Send the weekly summary to my boss")
    summary = z.task(
        "summary",
        "Send the weekly summary to my boss",
        created=slots[0] - timedelta(days=5),
        kind=TaskKind.RECURRING,
        recur_mode=RecurMode.SCHEDULED,
        cadence="every Friday by 5pm",
        rrule="FREQ=WEEKLY;BYDAY=FR",
        recur_dtstart=(slots[0] - timedelta(days=7)).astimezone(tz).replace(tzinfo=None),
        lead_minutes=int(LEAD.total_seconds() // 60),
        recur_firmness=Firmness.HARD,
        last_spawned_slot=slots[-1],
        priority=Priority.HIGH,
        motive=Motive.HAVE_TO,
        achievable=Achievable.YES,
        estimate_minutes=30,
        drain=Level.LOW,
        pattern_id=summary_pattern.id,
        sources={"priority": user},
    )
    instances = []
    for week, slot in enumerate(slots):
        inst = z.task(
            f"summary_w{week}",
            summary.title,
            created=slot - LEAD,
            parent=summary,
            occurrence_at=slot,
            deadline=slot,
            deadline_firmness=Firmness.HARD,
            achievable=Achievable.YES,
            estimate_minutes=30,
            drain=Level.LOW,
            pattern_id=summary_pattern.id,
        )
        record(
            session,
            EventType.SPAWNED,
            at=slot - LEAD,
            task_id=inst.id,
            payload={"recurring_id": summary.id, "occurrence_at": slot.isoformat()},
        )
        instances.append(inst)
    z.complete(instances[0], slots[0] - timedelta(hours=3), Motive.HAVE_TO, actual_minutes=25)
    # Week 1 was left open, so when week 3 spawned there were already two live.
    missed_at = slots[3] - LEAD
    instances[1].status = Status.MISSED
    instances[1].closed_at = missed_at
    instances[1].updated_at = missed_at
    record(
        session,
        EventType.MISSED,
        at=missed_at,
        task_id=instances[1].id,
        payload={"recurring_id": summary.id, "replaced_by": instances[3].id},
    )
    z.complete(
        instances[2],
        slots[3] - timedelta(days=1),
        Motive.HAVE_TO,
        actual_minutes=40,
        reflection="Late. Had to reconstruct the week from memory.",
    )

    # --- Flexible recurrence ---
    def flexible(handle: str, title: str, done_days_ago: list[float], **fields: Any) -> Task:
        pat = z.pattern(title)
        parent = z.task(
            handle,
            title,
            created=z.ago(days=40),
            kind=TaskKind.RECURRING,
            recur_mode=RecurMode.FLEXIBLE,
            pattern_id=pat.id,
            **fields,
        )
        for n, days in enumerate(sorted(done_days_ago, reverse=True)):
            done_at = z.ago(days=days)
            inst = z.task(
                f"{handle}_done{n}",
                title,
                created=done_at,
                parent=parent,
                achievable=Achievable.YES,
                estimate_minutes=fields.get("estimate_minutes"),
                pattern_id=pat.id,
            )
            z.complete(
                inst,
                done_at,
                fields.get("motive"),
                actual_minutes=fields.get("estimate_minutes"),
            )
        gap = fields.get("gap_days")
        if gap and done_days_ago:
            parent.snooze_until = z.ago(days=min(done_days_ago)) + timedelta(days=gap)
        return parent

    flexible(
        "laundry",
        "Do the laundry",
        [23, 17, 11, 6],
        cadence="about every five days",
        gap_days=5,
        motive=Motive.SHOULD,
        priority=Priority.NORMAL,
        achievable=Achievable.YES,
        estimate_minutes=40,
        activation=Level.LOW,
        drain=Level.LOW,
        conditions=["at-home"],
    )
    flexible(
        "run",
        "Go for a run",
        [12, 9, 5, 1],
        cadence="twice a week, never two days in a row",
        gap_days=2,
        motive=Motive.SHOULD,
        priority=Priority.NORMAL,
        achievable=Achievable.YES,
        estimate_minutes=45,
        activation=Level.MEDIUM,
        drain=Level.MEDIUM,
        conditions=["good-weather"],
    )
    flexible(
        "docs",
        "Read other teams' design docs",
        [20],
        cadence="when there's free time",
        motive=Motive.SHOULD,
        priority=Priority.LOW,
        achievable=Achievable.YES,
        estimate_minutes=30,
        activation=Level.LOW,
        drain=Level.LOW,
    )

    session.flush()
    return z.handles
