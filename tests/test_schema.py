"""The schema itself: migrations, constraints, column types, full-text search."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from task_orchard.core.enums import Firmness, Level, Priority, RecurMode, Status, TaskKind
from task_orchard.db.engine import alembic_config, include_name, init_db
from task_orchard.db.models import Base, Completion, Pattern, Task, TaskBlock
from tests.conftest import NOW


def make_task(**fields) -> Task:
    fields.setdefault("title", "A task")
    fields.setdefault("created_at", NOW)
    fields.setdefault("updated_at", NOW)
    return Task(**fields)


def scheduled_fields(**overrides) -> dict:
    fields = dict(
        kind=TaskKind.RECURRING,
        recur_mode=RecurMode.SCHEDULED,
        rrule="FREQ=WEEKLY;BYDAY=FR",
        recur_dtstart=datetime(2026, 3, 13, 17, 0),
        lead_minutes=2880,
        recur_firmness=Firmness.HARD,
        last_spawned_slot=NOW,
    )
    fields.update(overrides)
    return fields


def test_all_tables_exist(engine: sa.Engine) -> None:
    names = set(sa.inspect(engine).get_table_names())
    assert {
        "tasks",
        "task_blocks",
        "completions",
        "events",
        "patterns",
        "profile_notes",
        "tasks_fts",
        "patterns_fts",
    } <= names


def test_models_match_migrations(engine: sa.Engine) -> None:
    """If this fails, db/models.py changed without a new Alembic revision.

    Note: Alembic does not compare CHECK constraints. A changed CHECK must be
    migrated by hand and is not caught here.
    """
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_name": include_name})
        assert compare_metadata(ctx, Base.metadata) == []


def test_migrations_downgrade_and_upgrade(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "base")
    assert set(sa.inspect(engine).get_table_names()) == {"alembic_version"}
    init_db(engine)
    assert "tasks_fts" in sa.inspect(engine).get_table_names()


def test_init_db_is_idempotent(engine: sa.Engine) -> None:
    init_db(engine)
    init_db(engine)


def test_round_trip_of_typed_columns(session: Session) -> None:
    task = make_task(
        priority=Priority.HIGH,
        activation=Level.HIGH,
        drain=Level.LOW,
        deadline=NOW + timedelta(days=2),
        deadline_firmness=Firmness.HARD,
        conditions=["at-home"],
        field_sources={"priority": "user"},
    )
    session.add(task)
    session.commit()
    session.expire_all()

    loaded = session.get(Task, task.id)
    assert loaded.priority is Priority.HIGH
    assert loaded.activation is Level.HIGH
    assert loaded.status is Status.OPEN
    assert loaded.deadline == NOW + timedelta(days=2)
    assert loaded.deadline.tzinfo is not None
    assert loaded.conditions == ["at-home"]
    assert loaded.field_sources == {"priority": "user"}


def test_json_columns_track_in_place_changes(session: Session) -> None:
    task = make_task()
    session.add(task)
    session.commit()
    task.conditions.append("good-weather")
    task.field_sources["drain"] = "guess"
    session.commit()
    session.expire_all()
    loaded = session.get(Task, task.id)
    assert loaded.conditions == ["good-weather"]
    assert loaded.field_sources == {"drain": "guess"}


def test_naive_datetimes_are_refused(session: Session) -> None:
    session.add(make_task(created_at=datetime(2026, 3, 18, 10, 0)))
    with pytest.raises(StatementError, match="aware datetime"):
        session.flush()


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"title": "   "}, id="blank title"),
        pytest.param({"estimate_minutes": 0}, id="zero estimate"),
        pytest.param({"deadline": NOW}, id="deadline without firmness"),
        pytest.param({"deadline_firmness": Firmness.SOFT}, id="firmness without deadline"),
        pytest.param({"status": Status.DONE}, id="closed without closed_at"),
        pytest.param({"closed_at": NOW}, id="open with closed_at"),
        pytest.param({"status": Status.MISSED, "closed_at": NOW}, id="missed but not an instance"),
        pytest.param({"occurrence_at": NOW}, id="occurrence without parent"),
        pytest.param({"kind": TaskKind.RECURRING}, id="recurring without mode"),
        pytest.param({"recur_mode": RecurMode.FLEXIBLE}, id="mode on a plain task"),
        pytest.param({"cadence": "weekly"}, id="cadence on a plain task"),
        pytest.param({"gap_days": 3}, id="gap on a plain task"),
        pytest.param({"rrule": "FREQ=DAILY"}, id="rrule on a plain task"),
        pytest.param(scheduled_fields(rrule=None), id="scheduled without rrule"),
        pytest.param(scheduled_fields(last_spawned_slot=None), id="scheduled without last slot"),
        pytest.param(scheduled_fields(gap_days=3), id="gap on a scheduled task"),
        pytest.param(
            scheduled_fields(deadline=NOW, deadline_firmness=Firmness.HARD),
            id="recurring with a deadline",
        ),
        pytest.param(
            scheduled_fields(status=Status.DONE, closed_at=NOW), id="recurring marked done"
        ),
        pytest.param(
            {"kind": TaskKind.RECURRING, "recur_mode": RecurMode.FLEXIBLE, "rrule": "FREQ=DAILY"},
            id="rrule on a flexible task",
        ),
    ],
)
def test_check_constraints_reject(session: Session, fields: dict) -> None:
    session.add(make_task(**fields))
    with pytest.raises(IntegrityError):
        session.flush()


def test_out_of_range_scales_are_refused_twice_over(session: Session) -> None:
    """Python refuses a value outside the enum; the database refuses it independently."""
    session.add(make_task(priority=9))
    with pytest.raises(StatementError, match="not a valid Priority"):
        session.flush()
    session.rollback()

    insert = sa.text(
        "INSERT INTO tasks (title, kind, status, achievable, conditions, triaged, field_sources,"
        " created_at, updated_at, priority, drain)"
        " VALUES ('x', 'task', 'open', 'unknown', '[]', 0, '{}',"
        " '2026-03-18 10:00:00', '2026-03-18 10:00:00', :priority, :drain)"
    )
    session.execute(insert, {"priority": 4, "drain": 3})
    for bad in ({"priority": 9, "drain": 1}, {"priority": 1, "drain": 0}):
        with pytest.raises(IntegrityError):
            session.execute(insert, bad)
        session.rollback()


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({}, id="bare task"),
        pytest.param(scheduled_fields(), id="scheduled recurring"),
        pytest.param(
            {"kind": TaskKind.RECURRING, "recur_mode": RecurMode.FLEXIBLE, "gap_days": 2.5},
            id="flexible recurring",
        ),
        pytest.param(
            {"kind": TaskKind.RECURRING, "recur_mode": RecurMode.FLEXIBLE, "cadence": "sometimes"},
            id="flexible with only a cadence",
        ),
        pytest.param({"status": Status.DROPPED, "closed_at": NOW}, id="dropped task"),
    ],
)
def test_check_constraints_accept(session: Session, fields: dict) -> None:
    session.add(make_task(**fields))
    session.flush()


def test_one_instance_per_slot(session: Session) -> None:
    parent = make_task(**scheduled_fields())
    session.add(parent)
    session.flush()

    def instance() -> Task:
        return make_task(
            parent_id=parent.id,
            occurrence_at=NOW,
            deadline=NOW,
            deadline_firmness=Firmness.HARD,
        )

    session.add(instance())
    session.flush()
    session.add(instance())
    with pytest.raises(IntegrityError):
        session.flush()


def test_children_without_a_slot_are_not_limited(session: Session) -> None:
    parent = make_task()
    session.add(parent)
    session.flush()
    session.add_all([make_task(parent_id=parent.id), make_task(parent_id=parent.id)])
    session.flush()


def test_foreign_keys_are_enforced(session: Session) -> None:
    session.add(make_task(parent_id=9999))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_task_cannot_block_itself(session: Session) -> None:
    task = make_task()
    session.add(task)
    session.flush()
    session.add(TaskBlock(blocker_id=task.id, blocked_id=task.id, created_at=NOW))
    with pytest.raises(IntegrityError):
        session.flush()


def test_one_completion_per_task(session: Session) -> None:
    task = make_task(status=Status.DONE, closed_at=NOW)
    session.add(task)
    session.flush()
    session.add(Completion(task_id=task.id, completed_at=NOW))
    session.flush()
    session.add(Completion(task_id=task.id, completed_at=NOW))
    with pytest.raises(IntegrityError):
        session.flush()


def test_pattern_names_are_unique_ignoring_case(session: Session) -> None:
    session.add(Pattern(name="Phone calls", created_at=NOW, updated_at=NOW))
    session.flush()
    session.add(Pattern(name="phone CALLS", created_at=NOW, updated_at=NOW))
    with pytest.raises(IntegrityError):
        session.flush()


def test_pattern_learned_note_is_capped(session: Session) -> None:
    session.add(Pattern(name="Long", learned="x" * 601, created_at=NOW, updated_at=NOW))
    with pytest.raises(IntegrityError):
        session.flush()


def _search(session: Session, table: str, query: str) -> list[int]:
    rows = session.execute(
        sa.text(f"SELECT rowid FROM {table} WHERE {table} MATCH :q ORDER BY rowid"), {"q": query}
    )
    return [row[0] for row in rows]


def test_full_text_search_follows_inserts_and_updates(session: Session) -> None:
    task = make_task(title="Call the dentist", notes="ask about the crown")
    other = make_task(title="Buy milk")
    session.add_all([task, other])
    session.commit()

    assert _search(session, "tasks_fts", "dentist") == [task.id]
    assert _search(session, "tasks_fts", "crown") == [task.id]
    # Porter stemming: "calling" finds "Call".
    assert _search(session, "tasks_fts", "calling") == [task.id]

    task.title = "Ring the orthodontist"
    session.commit()
    assert _search(session, "tasks_fts", "dentist") == []
    assert _search(session, "tasks_fts", "orthodontist") == [task.id]


def test_full_text_search_covers_patterns(session: Session) -> None:
    pattern = Pattern(
        name="Interface specs",
        learned="Needs an uninterrupted morning.",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(pattern)
    session.commit()
    assert _search(session, "patterns_fts", "uninterrupted") == [pattern.id]
