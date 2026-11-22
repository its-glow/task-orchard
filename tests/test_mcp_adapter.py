"""SPEC 10.3: the MCP adapter is generated from the catalogue."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from task_orchard.core.service import Orchard
from task_orchard.mcp.server import build_server
from task_orchard.tools import catalogue


async def test_the_server_lists_the_catalogue_plus_get_guide(orchard: Orchard) -> None:
    tools = catalogue(orchard)
    async with Client(build_server(orchard)) as client:
        listed = {t.name: t for t in await client.list_tools()}
    assert set(listed) == {t.name for t in tools} | {"get_guide"}
    for tool in tools:
        assert listed[tool.name].description == tool.description, tool.name
        read_only = listed[tool.name].annotations
        assert bool(read_only and read_only.read_only_hint) == tool.read_only, tool.name


async def test_a_read_a_write_and_an_error_round_trip(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        captured = await client.call_tool("capture_tasks", {"items": [{"title": "Buy stamps"}]})
        task_id = captured.data["captured"][0]["id"]

        read = await client.call_tool("get_task", {"task_id": task_id})
        assert read.data == orchard.get_task(task_id)

        with pytest.raises(ToolError) as caught:
            await client.call_tool("get_task", {"task_id": 999})
    problem = json.loads(str(caught.value))
    assert problem["error"] == "not_found" and problem["hint"]


async def test_a_nested_input_schema_round_trips(orchard: Orchard) -> None:
    """subdivide_task takes a list of objects, the first nested input schema."""
    parent = orchard.create_task("Plan the party")["task"]["id"]
    async with Client(build_server(orchard)) as client:
        result = await client.call_tool(
            "subdivide_task",
            {
                "parent_id": parent,
                "children": [
                    {"title": "Invite", "achievable": "Yes"},
                    {"title": "Cake", "estimate_minutes": 30, "after": [1]},
                ],
            },
        )
    invite, cake = result.data["children"]
    assert cake["blocked_by"] == [{"id": invite["id"], "title": "Invite"}]
    assert cake["estimate_minutes"] == 30


async def test_the_recurrence_object_round_trips(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        result = await client.call_tool(
            "create_task",
            {
                "title": "Pay rent",
                "recurrence": {"mode": "scheduled", "rrule": "FREQ=MONTHLY;BYMONTHDAY=1"},
            },
        )
    assert result.data["task"]["recurring"]["mode"] == "scheduled"
    assert result.data["next_slots"][0]["at"] == "2026-04-01T23:59"
