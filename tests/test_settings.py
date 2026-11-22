"""SPEC 3.1 and 8.19: the stored timezone and set_timezone."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from alembic import command

from task_orchard.config import load_settings
from task_orchard.core.clock import FixedClock
from task_orchard.core.errors import Invalid
from task_orchard.core.service import Orchard
from task_orchard.db.engine import alembic_config, init_db, make_engine, make_session_factory
from tests.helpers import counts, events, last_event_id, make


def test_set_timezone_stores_the_zone_and_the_next_call_uses_it(orchard: Orchard) -> None:
    result = orchard.set_timezone("america/new_york")
    assert result["timezone"] == "America/New_York"
    assert result["now"]["at"] == "2026-03-18T06:00"  # 10:00 UTC, EDT
    task = make(orchard, "T", deadline="today")
    assert orchard.get_task(task)["task"]["due"]["at"] == "2026-03-18T23:59"
    assert orchard.get_task(task)["now"]["at"].startswith("2026-03-18T06:00")


def test_set_timezone_writes_one_event_and_nothing_when_unchanged(orchard: Orchard) -> None:
    since = last_event_id(orchard)
    orchard.set_timezone("Asia/Tokyo")
    orchard.set_timezone("Europe/Paris")
    assert events(orchard, since) == [
        ("settings_saved", None, {"timezone": {"from": None, "to": "Asia/Tokyo"}}),
        ("settings_saved", None, {"timezone": {"from": "Asia/Tokyo", "to": "Europe/Paris"}}),
    ]
    before = counts(orchard)
    orchard.set_timezone("Europe/Paris")
    assert counts(orchard) == before


@pytest.mark.parametrize("name", ["Mars/Olympus", "", "  ", "GMT+5:30", "../etc/passwd"])
def test_an_unknown_zone_is_invalid(orchard: Orchard, name: str) -> None:
    before = counts(orchard)
    with pytest.raises(Invalid) as caught:
        orchard.set_timezone(name)
    assert "Europe/London" in caught.value.hint
    assert counts(orchard) == before


def test_the_environment_still_wins(engine: sa.Engine, clock: FixedClock) -> None:
    fixed = Orchard(make_session_factory(engine), clock, ZoneInfo("Europe/London"), tz_fixed=True)
    result = fixed.set_timezone("Asia/Tokyo")
    assert result["timezone"] == "Asia/Tokyo"  # stored
    assert "TASK_ORCHARD_TZ" in result["note"]
    assert fixed.get_critical()["now"]["at"].startswith("2026-03-18T10:00")  # still London
    # Another client without the variable picks up the stored zone.
    free = Orchard(make_session_factory(engine), clock, ZoneInfo("UTC"))
    assert free.get_critical()["now"]["at"].startswith("2026-03-18T19:00")


def test_roll_forward_runs_in_the_stored_zone(orchard: Orchard, clock: FixedClock) -> None:
    orchard.set_timezone("Asia/Tokyo")  # 19:00 on Wednesday there
    daily = orchard.create_task(
        "Stretch",
        recurrence={
            "mode": "scheduled",
            "rrule": "FREQ=DAILY",
            "due_time": "08:00",
            "lead_days": 0,
        },
    )
    assert daily["next_slots"][0]["at"] == "2026-03-19T08:00"
    clock.advance(hours=13)  # 23:00 UTC = 08:00 Thursday in Tokyo
    orchard.get_critical()
    detail = orchard.get_task(daily["task"]["id"])["task"]
    assert detail["recurring"]["next_slot"]["at"] == "2026-03-20T08:00"


def test_stored_timezone_is_reported(orchard: Orchard) -> None:
    assert orchard.stored_timezone() is None
    orchard.set_timezone("Europe/Berlin")
    assert orchard.stored_timezone() == "Europe/Berlin"


def test_settings_record_whether_the_zone_was_explicit() -> None:
    explicit = load_settings({"TASK_ORCHARD_TZ": "Asia/Tokyo", "TASK_ORCHARD_DB": "/x.db"})
    assert explicit.timezone_explicit is True
    detected = load_settings({"TZ": "Asia/Tokyo", "TASK_ORCHARD_DB": "/x.db"})
    assert detected.timezone_explicit is False


def test_an_existing_database_is_upgraded(tmp_path: Path) -> None:
    """Real databases were created at 0001; starting the app must add the table."""
    engine = make_engine(tmp_path / "old.db")
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0001")
    assert "settings" not in sa.inspect(engine).get_table_names()
    init_db(engine)
    assert "settings" in sa.inspect(engine).get_table_names()
    engine.dispose()
