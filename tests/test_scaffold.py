"""The parts of the scaffold that already work: enums, clock, config, the
transaction wrapper, the demo seed, guides and the MCP server shell.

These are Phase 0. Later phases add their own test modules (SPEC section 11).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastmcp import Client
from fastmcp.exceptions import ToolError

from task_orchard.config import load_settings
from task_orchard.core.clock import FixedClock
from task_orchard.core.enums import EventType, Level, Priority, RecurMode, Status
from task_orchard.core.errors import CycleError, NotFound
from task_orchard.core.events import record
from task_orchard.core.service import Orchard
from task_orchard.db.models import Completion, Event, Pattern, ProfileNote, Task, TaskBlock
from task_orchard.devtools.seed import DatabaseNotEmpty, seed_demo
from task_orchard.guides import GuideFormatError, load_guides, parse_guide
from task_orchard.mcp.server import build_server
from tests.conftest import NOW, TZ

# --- Enums ---


def test_scales_are_spoken_by_name() -> None:
    assert Priority.parse("high") is Priority.HIGH
    assert Priority.parse(" Critical ") is Priority.CRITICAL
    assert Priority.HIGH.label == "high"
    assert Level.parse(2) is Level.MEDIUM
    assert max(Priority.LOW, Priority.CRITICAL) is Priority.CRITICAL


def test_parsing_an_unknown_name_lists_the_choices() -> None:
    with pytest.raises(ValueError, match="low, normal, high, critical"):
        Priority.parse("urgent")


# --- Clock ---


def test_fixed_clock_moves_only_when_told() -> None:
    clock = FixedClock(NOW)
    assert clock.now() == NOW
    clock.advance(days=3)
    assert clock.now() == NOW + timedelta(days=3)
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 1, 1))


# --- Config ---


def test_settings_from_environment(tmp_path: Path) -> None:
    env = {"TASK_ORCHARD_DB": str(tmp_path / "x.db"), "TASK_ORCHARD_TZ": "America/New_York"}
    settings = load_settings(env)
    assert settings.db_path == tmp_path / "x.db"
    assert settings.timezone.key == "America/New_York"
    assert settings.timezone_detected


def test_default_database_path_follows_xdg(tmp_path: Path) -> None:
    settings = load_settings({"XDG_DATA_HOME": str(tmp_path), "TASK_ORCHARD_TZ": "UTC"})
    assert settings.db_path == tmp_path / "task-orchard" / "orchard.db"


def test_a_wrong_explicit_timezone_fails_loudly() -> None:
    with pytest.raises(Exception, match="Nowhere"):
        load_settings({"TASK_ORCHARD_TZ": "Mars/Nowhere"})


# --- Errors ---


def test_errors_carry_a_hint_for_the_model() -> None:
    err = NotFound("task", 42)
    assert err.to_dict()["error"] == "not_found"
    assert "42" in err.to_dict()["message"]
    assert "hint" in err.to_dict()


def test_cycle_error_shows_the_loop() -> None:
    err = CycleError([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}, {"id": 1, "title": "A"}])
    assert "#1 -> #2 -> #1" in err.message
    assert err.to_dict()["details"]["path"][1]["title"] == "B"


# --- The transaction wrapper ---


def test_a_transaction_commits(orchard: Orchard) -> None:
    with orchard.tx() as tx:
        tx.session.add(Task(title="Kept", created_at=tx.now, updated_at=tx.now))
    with orchard.tx() as tx:
        assert tx.session.scalar(sa.select(sa.func.count()).select_from(Task)) == 1


def test_a_failed_transaction_leaves_nothing_behind(orchard: Orchard) -> None:
    with pytest.raises(RuntimeError), orchard.tx() as tx:
        task = Task(title="Discarded", created_at=tx.now, updated_at=tx.now)
        tx.session.add(task)
        tx.session.flush()
        record(tx.session, EventType.CREATED, at=tx.now, task_id=task.id)
        raise RuntimeError("boom")
    with orchard.tx() as tx:
        assert tx.session.scalar(sa.select(sa.func.count()).select_from(Task)) == 0
        assert tx.session.scalar(sa.select(sa.func.count()).select_from(Event)) == 0


def test_now_comes_from_the_clock(orchard: Orchard, clock: FixedClock) -> None:
    with orchard.tx() as tx:
        assert tx.now == NOW
        assert tx.tz is TZ
    clock.advance(hours=5)
    with orchard.tx() as tx:
        assert tx.now == NOW + timedelta(hours=5)


def test_open_creates_and_migrates_a_new_database(tmp_path: Path) -> None:
    settings = load_settings(
        {"TASK_ORCHARD_DB": str(tmp_path / "new" / "orchard.db"), "TASK_ORCHARD_TZ": "UTC"}
    )
    orchard = Orchard.open(settings)
    with orchard.tx() as tx:
        assert tx.session.scalar(sa.select(sa.func.count()).select_from(Task)) == 0
        assert tx.now.tzinfo is UTC
    orchard.engine.dispose()


# --- The demo seed ---


def test_seed_covers_every_shape(orchard: Orchard, seeded: dict[str, int]) -> None:
    with orchard.tx() as tx:
        s = tx.session
        get = lambda handle: s.get(Task, seeded[handle])  # noqa: E731

        assert not get("inbox_run").triaged
        assert get("release").priority is Priority.CRITICAL
        assert get("cert").priority is Priority.LOW
        assert get("taxes").snooze_until > tx.now
        assert get("obs_backend").status is Status.DONE
        assert get("obs_backend").completion.actual_minutes == 150

        summary = get("summary")
        assert summary.is_recurring
        assert not get("summary_w3").is_recurring
        assert summary.recur_mode is RecurMode.SCHEDULED
        statuses = [get(f"summary_w{n}").status for n in range(4)]
        assert statuses == [Status.DONE, Status.MISSED, Status.DONE, Status.OPEN]
        assert get("summary_w3").occurrence_at == summary.last_spawned_slot
        assert get("summary_w3").deadline <= tx.now

        assert get("laundry").snooze_until < tx.now  # awake again
        assert get("run").snooze_until > tx.now  # still resting
        assert get("docs").gap_days is None

        links = {(b.blocker_id, b.blocked_id) for b in s.scalars(sa.select(TaskBlock))}
        assert (seeded["cert"], seeded["release_publish"]) in links
        assert (seeded["budget"], seeded["offsite"]) in links

        assert s.scalar(sa.select(sa.func.count()).select_from(Pattern)) >= 5
        assert s.get(Completion, 1) is not None
        charter = s.scalar(sa.select(ProfileNote).where(ProfileNote.topic == "CHARTER"))
        assert charter is not None  # topics compare without regard to case


def test_seed_slots_fall_on_friday_at_five_local(orchard: Orchard, seeded: dict[str, int]) -> None:
    with orchard.tx() as tx:
        for n in range(4):
            slot = tx.session.get(Task, seeded[f"summary_w{n}"]).occurrence_at.astimezone(TZ)
            assert (slot.weekday(), slot.hour, slot.minute) == (4, 17, 0)


def test_every_seeded_task_has_a_created_event(orchard: Orchard, seeded: dict[str, int]) -> None:
    with orchard.tx() as tx:
        created = set(
            tx.session.scalars(sa.select(Event.task_id).where(Event.type == EventType.CREATED))
        )
        assert created == set(tx.session.scalars(sa.select(Task.id)))


def test_seed_refuses_a_database_with_tasks(orchard: Orchard, seeded: dict[str, int]) -> None:
    with pytest.raises(DatabaseNotEmpty), orchard.tx() as tx:
        seed_demo(tx.session, tx.now, tx.tz)


# --- Guides ---


def test_shipped_guides_parse_and_include_the_overview() -> None:
    guides = load_guides()
    assert "overview" in guides
    assert guides["overview"].description
    assert guides["overview"].body.startswith("# Task Orchard")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("no header\n", id="no header"),
        pytest.param("---\nname: x\ndescription: y\n", id="header never closed"),
        pytest.param("---\nname: x\n---\nbody\n", id="missing description"),
        pytest.param("---\nname: x\ndescription: y\n---\n   \n", id="empty body"),
    ],
)
def test_malformed_guides_are_rejected(text: str) -> None:
    with pytest.raises(GuideFormatError):
        parse_guide(text)


# --- The MCP server shell ---


async def test_server_lists_tools_and_prompts(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        assert "get_guide" in {tool.name for tool in await client.list_tools()}
        assert "overview" in {prompt.name for prompt in await client.list_prompts()}


async def test_get_guide_lists_and_returns_guides(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        listing = (await client.call_tool("get_guide", {})).data
        assert {"name", "description"} <= listing["guides"][0].keys()

        overview = (await client.call_tool("get_guide", {"name": "overview"})).data
        assert "Task trees" in overview["text"]


async def test_tool_errors_reach_the_model_as_json_with_a_hint(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("get_guide", {"name": "nonsense"})
        problem = json.loads(str(caught.value))
        assert problem["error"] == "not_found"
        assert "overview" in problem["hint"]


async def test_overview_prompt_is_the_guide_text(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        result = await client.get_prompt("overview")
        assert "Task trees" in result.messages[0].content.text


async def test_instructions_warn_when_the_timezone_is_assumed(
    orchard: Orchard, tmp_path: Path
) -> None:
    from dataclasses import replace

    settings = load_settings({"TASK_ORCHARD_DB": str(tmp_path / "x.db"), "TASK_ORCHARD_TZ": "UTC"})
    quiet = build_server(orchard, settings)
    loud = build_server(orchard, replace(settings, timezone_detected=False))
    assert "set_timezone" not in quiet.instructions
    assert "set_timezone" in loud.instructions
    orchard.set_timezone("Europe/London")
    settled = build_server(orchard, replace(settings, timezone_detected=False))
    assert "set_timezone" not in settled.instructions
