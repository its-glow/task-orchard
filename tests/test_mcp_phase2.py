"""Phase 2 through the in-memory MCP client: tools, instructions and guides."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from task_orchard.core.service import NOTE_MAX_CHARS, Orchard
from task_orchard.guides import load_guides
from task_orchard.mcp.server import build_server

PHASE_2_TOOLS = {"get_candidates", "get_critical", "read_profile", "write_profile_note"}
GUIDE_MAX_WORDS = 600


async def call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    return (await client.call_tool(tool, args)).data


async def test_tools_are_listed(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools.keys() >= PHASE_2_TOOLS
    for name in PHASE_2_TOOLS:
        assert len(tools[name].description.split()) <= 80, name
    for name in ("get_candidates", "get_critical", "read_profile"):
        assert tools[name].annotations.read_only_hint is True
    energy = tools["get_candidates"].input_schema["properties"]["energy"]
    assert {"enum": ["low", "medium", "high"], "type": "string"} in energy["anyOf"]


async def test_every_tool_works_through_the_client(
    orchard: Orchard, seeded: dict[str, int]
) -> None:
    async with Client(build_server(orchard)) as client:
        shortlist = await call(client, "get_candidates", limit=5, energy="Low")
        assert len(shortlist["candidates"]) == 5
        assert shortlist["pool"]["shown"] == 5 and shortlist["inbox"] == 3

        critical = await call(client, "get_critical", horizon_days=2)
        assert seeded["cert"] in {row["id"] for row in critical["critical"]}

        profile = await call(client, "write_profile_note", topic="sleep", text="Bed by 11.")
        assert {"topic", "text", "updated"} <= profile["notes"][1].keys()
        assert (await call(client, "read_profile"))["charter"]["text"]

        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "write_profile_note", {"topic": "x", "text": "y" * (NOTE_MAX_CHARS + 1)}
            )
        problem = json.loads(str(caught.value))
        assert problem["error"] == "profile_full" and problem["hint"]


def test_instructions_send_the_agent_to_the_profile_first(orchard: Orchard) -> None:
    text = build_server(orchard).instructions
    assert text.index("read_profile") < text.index('"overview"')


def test_phase_2_guides_ship_and_stay_short() -> None:
    guides = load_guides()
    for name in ("triage", "what-now"):
        assert name in guides
        assert len(guides[name].body.split()) <= GUIDE_MAX_WORDS, name


async def test_guides_are_prompts(orchard: Orchard) -> None:
    async with Client(build_server(orchard)) as client:
        names = {prompt.name for prompt in await client.list_prompts()}
    assert {"overview", "triage", "what-now"} <= names


def test_every_guide_ships_and_stays_short() -> None:
    """SPEC 9: each job guide under about 600 words. The overview explains the
    ideas rather than one job, and is allowed a little more."""
    guides = load_guides()
    assert {
        "overview", "triage", "what-now", "subdivide", "complete-and-reflect",
        "onboarding", "plan-time-block", "housekeeping", "weekly-review",
    } <= set(guides)  # fmt: skip
    for name, guide in guides.items():
        limit = 800 if name == "overview" else GUIDE_MAX_WORDS
        assert len(guide.body.split()) <= limit, name
