"""The app's system prompt and the time line on each user message (SPEC 10.2).

The system prompt is built once per session and never changed after:

1. the preamble (agent/preamble.md), with the timezone warning when no zone
   was detected;
2. the overview guide;
3. every other guide, in name order, under "## Guide: <name>";
4. the profile, as read when the session started.

Parts 1 to 3 form the first block, identical for every session until a
release changes a guide, so it can be cached. Part 4 is the second block.
Nothing that varies between sessions appears before it, and the time appears
nowhere in the system prompt: it travels with each user message instead.
"""

from __future__ import annotations

import json
from datetime import datetime
from importlib import resources
from typing import Any
from zoneinfo import ZoneInfo

from task_orchard.config import TIMEZONE_WARNING
from task_orchard.core import timefmt
from task_orchard.guides import Guide

OVERVIEW = "overview"
PROFILE_HEADING = "## The user's profile"


def preamble() -> str:
    return (resources.files("task_orchard.agent") / "preamble.md").read_text(encoding="utf-8")


def stable_block(guides: dict[str, Guide], *, timezone_detected: bool) -> str:
    parts = [preamble().strip()]
    if not timezone_detected:
        parts.append(TIMEZONE_WARNING)
    if OVERVIEW in guides:
        parts.append(guides[OVERVIEW].body.strip())
    for name in sorted(guides):
        if name == OVERVIEW:
            continue
        guide = guides[name]
        parts.append(f"## Guide: {guide.name}\n\n{guide.description}\n\n{guide.body.strip()}")
    return "\n\n".join(parts) + "\n"


def profile_block(profile: dict[str, Any]) -> str:
    # `now` is when the session started. Leaving it out keeps a stale time out of
    # the prompt; the current time comes with each user message.
    shown = {key: value for key, value in profile.items() if key != "now"}
    return f"{PROFILE_HEADING}\n\n{json.dumps(shown, ensure_ascii=False, indent=1)}\n"


def system_prompt(
    guides: dict[str, Guide], profile: dict[str, Any], *, timezone_detected: bool
) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": stable_block(guides, timezone_detected=timezone_detected)},
        {"type": "text", "text": profile_block(profile)},
    ]


def now_line(now: datetime, tz: ZoneInfo) -> str:
    """[Now: Fri 2026-11-20T17:00, Europe/London], from the stamp of SPEC 3.3."""
    stamp = timefmt.now_stamp(now, tz)
    return f"[Now: {stamp['day']} {stamp['at']}, {tz.key}]"
