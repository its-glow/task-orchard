"""Each Phase 1 tool through the in-memory MCP client: wiring, schema, errors."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from task_orchard.core.service import Orchard
from task_orchard.mcp.server import build_server

PHASE_1_TOOLS = {
    "capture_tasks",
    "create_task",
    "update_task",
    "link_tasks",
    "unlink_tasks",
    "get_task",
    "get_tree",
    "search_tasks",
}
DESCRIPTION_MAX_WORDS = 80


async def call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    return (await client.call_tool(tool, args)).data


async def problem(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    with pytest.raises(ToolError) as caught:
        await client.call_tool(tool, args)
    return json.loads(str(caught.value))


async def test_tools_are_listed_with_short_descriptions(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools.keys() >= PHASE_1_TOOLS
    for name in PHASE_1_TOOLS:
        assert len(tools[name].description.split()) <= DESCRIPTION_MAX_WORDS, name
    for name in ("get_task", "get_tree", "search_tasks"):
        assert tools[name].annotations.read_only_hint is True
    assert not (
        tools["create_task"].annotations and tools["create_task"].annotations.read_only_hint
    )
    priority = tools["create_task"].input_schema["properties"]["priority"]
    assert {"enum": ["low", "normal", "high", "critical"], "type": "string"} in priority["anyOf"]


async def test_every_tool_works_through_the_client(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        captured = await call(client, "capture_tasks", items=[{"title": "Desk", "notes": "IKEA"}])
        inbox_id = captured["captured"][0]["id"]

        parent = (await call(client, "create_task", title="Project", priority="High"))["task"]
        child = (
            await call(
                client,
                "create_task",
                title="Step",
                parent_id=parent["id"],
                deadline="friday",
                sources={"deadline": "user"},
            )
        )["task"]
        assert child["due"]["at"] == "2026-03-20T23:59"

        updated = await call(client, "update_task", task_id=inbox_id, motive="Must")
        assert updated["task"]["motive"] == "must"
        skipped = await call(client, "update_task", task_id=child["id"], clear=["deadline"])
        assert skipped["skipped"][0]["field"] == "deadline" and "hint" in skipped

        linked = await call(client, "link_tasks", blocker_id=inbox_id, blocked_id=parent["id"])
        assert linked["blocked"]["state"] == ["blocked", "has_open_children"]
        unlinked = await call(client, "unlink_tasks", blocker_id=inbox_id, blocked_id=parent["id"])
        assert "was_linked" not in unlinked

        assert (await call(client, "get_task", task_id=child["id"]))["task"]["parent"]["id"] == (
            parent["id"]
        )
        tree = await call(client, "get_tree")
        assert {n["id"] for n in tree["tree"]} == {inbox_id, parent["id"]}
        found = await call(client, "search_tasks", query="desk")
        assert found["results"][0]["id"] == inbox_id
        assert found["now"]["day"] == "Wed"


async def test_errors_arrive_as_json_with_a_hint(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        missing = await problem(client, "get_task", task_id=999)
        assert missing["error"] == "not_found" and missing["hint"]

        bad_time = await problem(client, "create_task", title="T", deadline="next week")
        assert bad_time["error"] == "invalid" and "+3d" in bad_time["hint"]

        bad_name = await problem(client, "create_task", title="T", priority="urgent")
        assert bad_name["error"] == "invalid" and "critical" in bad_name["hint"]

        a = (await call(client, "create_task", title="A"))["task"]["id"]
        b = (await call(client, "create_task", title="B", blocked_by=[a]))["task"]["id"]
        loop = await problem(client, "link_tasks", blocker_id=b, blocked_id=a)
        assert loop["error"] == "cycle"
        assert loop["details"]["path"][0]["status"] == "open"

        no_words = await problem(client, "search_tasks", query="*")
        assert no_words["error"] == "invalid"
