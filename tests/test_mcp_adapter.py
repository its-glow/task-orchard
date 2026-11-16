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
