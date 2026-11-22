"""SPEC 10.1: the tool catalogue and call_tool."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from task_orchard.core.service import Orchard
from task_orchard.db.models import Event, Task
from task_orchard.tools import Tool, call_tool, catalogue

# Section 8 order, as far as it is built.
BUILT = [
    "capture_tasks",
    "create_task",
    "update_task",
    "subdivide_task",
    "link_tasks",
    "unlink_tasks",
    "start_task",
    "complete_task",
    "snooze_task",
    "drop_tasks",
    "reopen_task",
    "merge_tasks",
    "get_task",
    "get_tree",
    "search_tasks",
    "get_candidates",
    "get_critical",
    "get_review_queue",
    "get_recent_activity",
    "find_patterns",
    "save_pattern",
    "read_profile",
    "write_profile_note",
    "set_timezone",
]
READ_ONLY = {
    "get_task",
    "get_tree",
    "search_tasks",
    "get_candidates",
    "get_critical",
    "get_review_queue",
    "get_recent_activity",
    "find_patterns",
    "read_profile",
}


def counts(orchard: Orchard) -> tuple[int, int]:
    with orchard.tx() as tx:
        tasks = tx.session.scalar(sa.select(sa.func.count()).select_from(Task))
        events = tx.session.scalar(sa.select(sa.func.count()).select_from(Event))
    return tasks, events


@pytest.fixture
def tools(orchard: Orchard) -> list[Tool]:
    return catalogue(orchard)


def test_one_tool_per_built_operation_in_section_8_order(tools: list[Tool]) -> None:
    assert [t.name for t in tools] == BUILT
    for tool in tools:
        assert callable(getattr(Orchard, tool.name)), tool.name
    assert "get_guide" not in {t.name for t in tools}


def test_read_only_is_set_for_exactly_the_reads(tools: list[Tool]) -> None:
    assert {t.name for t in tools if t.read_only} == READ_ONLY


def test_descriptions_are_short(tools: list[Tool]) -> None:
    for tool in tools:
        assert tool.description, tool.name
        assert len(tool.description.split()) <= 80, tool.name


def test_every_schema_is_an_object_with_described_parameters(tools: list[Tool]) -> None:
    for tool in tools:
        schema = tool.input_schema
        assert schema["type"] == "object", tool.name
        assert schema.get("additionalProperties") is False, tool.name
        for name, prop in schema["properties"].items():
            assert prop.get("description"), f"{tool.name}.{name}"
    required = {t.name: t.input_schema.get("required", []) for t in tools}
    assert required["get_task"] == ["task_id"]
    assert required["read_profile"] == []


def _enum_of(prop: dict[str, Any]) -> list[str] | None:
    if "enum" in prop:
        return prop["enum"]
    for option in prop.get("anyOf", []):
        if "enum" in option:
            return option["enum"]
    return None


def test_enumerated_parameters_list_their_names(tools: list[Tool]) -> None:
    by_name = {t.name: t for t in tools}
    create = by_name["create_task"].input_schema["properties"]
    assert _enum_of(create["priority"]) == ["low", "normal", "high", "critical"]
    assert _enum_of(create["motive"]) == ["want", "should", "must"]
    energy = by_name["get_candidates"].input_schema["properties"]["energy"]
    assert _enum_of(energy) == ["low", "medium", "high"]
    status = by_name["search_tasks"].input_schema["properties"]["status"]
    assert _enum_of(status) == ["open", "closed", "any"]


def test_names_are_still_matched_leniently(tools: list[Tool]) -> None:
    outcome = call_tool(tools, "create_task", {"title": "Renew passport", "motive": "Must"})
    assert not outcome.is_error, outcome.payload
    assert outcome.payload["task"]["fields"]["motive"]["value"] == "must"


def test_success_returns_the_methods_dict(
    orchard: Orchard, seeded: dict[str, int], tools: list[Tool]
) -> None:
    outcome = call_tool(tools, "search_tasks", {"query": "release", "limit": 5})
    assert outcome.is_error is False
    assert outcome.payload == orchard.search_tasks("release", limit=5)

    outcome = call_tool(tools, "get_candidates", {"energy": "low", "limit": 4})
    assert outcome.payload == orchard.get_candidates(limit=4, energy="low")

    outcome = call_tool(tools, "read_profile", {})
    assert outcome.payload == orchard.read_profile()


def test_a_write_changes_the_database(orchard: Orchard, tools: list[Tool]) -> None:
    outcome = call_tool(tools, "capture_tasks", {"items": [{"title": "Buy stamps"}]})
    assert not outcome.is_error
    assert orchard.search_tasks("stamps")["total"] == 1


def test_an_orchard_error_becomes_the_section_4_2_json(orchard: Orchard, tools: list[Tool]) -> None:
    outcome = call_tool(tools, "get_task", {"task_id": 999})
    assert outcome.is_error is True
    assert outcome.payload["error"] == "not_found"
    assert outcome.payload["message"] and outcome.payload["hint"]


def test_an_unknown_tool_is_invalid_and_lists_the_names(tools: list[Tool]) -> None:
    outcome = call_tool(tools, "plant_seeds", {"row": 1})
    assert outcome.is_error
    assert outcome.payload["error"] == "invalid"
    assert "capture_tasks" in outcome.payload["hint"]
    assert "write_profile_note" in outcome.payload["hint"]


def test_a_missing_argument_is_invalid_and_nothing_is_written(
    orchard: Orchard, tools: list[Tool]
) -> None:
    before = counts(orchard)
    outcome = call_tool(tools, "create_task", {"priority": "high"})
    assert outcome.is_error and outcome.payload["error"] == "invalid"
    assert "title" in outcome.payload["hint"]
    assert counts(orchard) == before


def test_a_wrong_type_is_invalid_and_nothing_is_written(
    orchard: Orchard, tools: list[Tool]
) -> None:
    before = counts(orchard)
    outcome = call_tool(tools, "create_task", {"title": "Fix the gate", "estimate_minutes": "lots"})
    assert outcome.is_error and outcome.payload["error"] == "invalid"
    assert "estimate_minutes" in outcome.payload["hint"]
    assert counts(orchard) == before


def test_a_number_written_as_a_string_is_accepted(orchard: Orchard, tools: list[Tool]) -> None:
    # Lax, as the MCP adapter is: a small model often quotes numbers.
    created = call_tool(tools, "create_task", {"title": "Fix the gate", "estimate_minutes": "30"})
    assert not created.is_error
    assert created.payload["task"]["fields"]["estimate_minutes"]["value"] == 30


def test_an_unknown_parameter_is_invalid_and_nothing_is_written(
    orchard: Orchard, tools: list[Tool]
) -> None:
    before = counts(orchard)
    outcome = call_tool(tools, "create_task", {"title": "Fix the gate", "colour": "green"})
    assert outcome.is_error and outcome.payload["error"] == "invalid"
    assert "colour" in outcome.payload["hint"]
    assert "estimate_minutes" in outcome.payload["hint"]  # names the real parameters
    assert counts(orchard) == before


def test_arguments_that_are_not_an_object_are_invalid(tools: list[Tool]) -> None:
    outcome = call_tool(tools, "get_task", ["12"])  # type: ignore[arg-type]
    assert outcome.is_error and outcome.payload["error"] == "invalid"


def test_a_bug_propagates(orchard: Orchard, tools: list[Tool], monkeypatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(orchard, "read_profile", broken)
    with pytest.raises(RuntimeError):
        call_tool(tools, "read_profile", {})


# --- Phase 3 tools, once each through call_tool (SPEC 11) ---


def test_phase_3_tools_round_trip(
    orchard: Orchard, seeded: dict[str, int], tools: list[Tool]
) -> None:
    def ok(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        outcome = call_tool(tools, name, arguments)
        assert not outcome.is_error, (name, outcome.payload)
        assert "now" in outcome.payload
        return outcome.payload

    parent = ok("create_task", {"title": "Plan the party"})["task"]["id"]
    children = ok(
        "subdivide_task",
        {
            "parent_id": parent,
            "children": [{"title": "Invite", "achievable": "Yes"}, {"title": "Cake", "after": [1]}],
        },
    )["children"]
    invite, cake = (c["id"] for c in children)
    assert ok("start_task", {"task_id": invite})["starts"] == 1
    assert ok("snooze_task", {"task_id": cake, "days": "2"})["task"]["deferred"] == 1
    done = ok(
        "complete_task",
        {"task_id": invite, "actual_minutes": "15", "felt_drain": "Low", "new_pattern": "Parties"},
    )
    assert [r["id"] for r in done["unblocked"]] == [cake]
    assert ok("reopen_task", {"task_id": invite})["task"]["id"] == invite
    assert ok("drop_tasks", {"task_ids": [parent], "reason": "cancelled"})["also_dropped"]
    found = ok("find_patterns", {"query": "parties"})["patterns"]
    assert [p["name"] for p in found] == ["Parties"]
    saved = ok("save_pattern", {"pattern_id": found[0]["id"], "learned": "Plan a week ahead."})
    assert saved["pattern"]["learned"] == "Plan a week ahead."


def test_phase_3_errors_round_trip(tools: list[Tool]) -> None:
    outcome = call_tool(tools, "complete_task", {})
    assert outcome.is_error and outcome.payload["error"] == "invalid"
    outcome = call_tool(tools, "snooze_task", {"task_id": 1, "days": 1.5})
    assert outcome.is_error and outcome.payload["error"] == "invalid"
    assert "days" in outcome.payload["hint"]
    outcome = call_tool(tools, "reopen_task", {"task_id": 1})
    assert outcome.is_error and outcome.payload["error"] == "not_found"


# --- Phase 4: the recurrence object through call_tool and MCP ---


def test_recurrence_round_trips(orchard: Orchard, tools: list[Tool]) -> None:
    spec = {"mode": "Scheduled", "rrule": "FREQ=WEEKLY;BYDAY=FR", "due_time": "17:00"}
    created = call_tool(tools, "create_task", {"title": "Summary", "recurrence": spec})
    assert not created.is_error, created.payload
    assert created.payload["next_slots"][0]["at"] == "2026-03-20T17:00"
    laundry = call_tool(
        tools,
        "create_task",
        {"title": "Laundry", "recurrence": {"mode": "flexible", "gap_days": "4"}},
    ).payload["task"]["id"]
    instance = call_tool(tools, "create_task", {"parent_id": laundry}).payload["task"]
    assert instance["title"] == "Laundry"
    updated = call_tool(
        tools, "update_task", {"task_id": laundry, "recurrence": {"cadence": "weekly-ish"}}
    )
    assert updated.payload["task"]["recurring"]["cadence"] == "weekly-ish"
    bad = call_tool(
        tools, "create_task", {"title": "X", "recurrence": {"mode": "flexible", "x": 1}}
    )
    assert bad.is_error and bad.payload["error"] == "invalid"


# --- Phase 5 tools, once each through call_tool ---


def test_phase_5_tools_round_trip(orchard: Orchard, tools: list[Tool]) -> None:
    def ok(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        outcome = call_tool(tools, name, arguments)
        assert not outcome.is_error, (name, outcome.payload)
        return outcome.payload

    keep = ok("create_task", {"title": "Buy stamps"})["task"]["id"]
    extra = ok("create_task", {"title": "buy stamps", "estimate_minutes": 10})["task"]["id"]
    queue = ok("get_review_queue", {})
    assert queue["duplicates"]["total"] == 1
    merged = ok("merge_tasks", {"source_id": extra, "into_id": keep})
    assert merged["task"]["estimate_minutes"] == 10
    activity = ok("get_recent_activity", {"days": "3"})
    assert activity["dropped_count"] == 1
    assert ok("set_timezone", {"name": "Europe/Paris"})["timezone"] == "Europe/Paris"
    bad = call_tool(tools, "get_recent_activity", {"days": 40})
    assert bad.is_error and bad.payload["error"] == "invalid"
