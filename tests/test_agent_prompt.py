"""SPEC 10.2: the app's system prompt and the time line on each user message."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from task_orchard.agent.loop import run_turn, start_session
from task_orchard.agent.model import BASIC, STRATEGIC
from task_orchard.agent.prompt import now_line, preamble, system_prompt
from task_orchard.config import TIMEZONE_WARNING
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from task_orchard.guides import load_guides
from tests.scripted import ScriptedModel, text


def models(*replies) -> dict[str, ScriptedModel]:
    return {BASIC: ScriptedModel(list(replies)), STRATEGIC: ScriptedModel([])}


def test_the_four_parts_in_order(orchard: Orchard, clock: FixedClock, seeded) -> None:
    session = start_session(orchard, clock, models())
    stable, profile = (block["text"] for block in session.system)
    guides = load_guides()

    assert stable.startswith(preamble().strip())
    positions = [stable.index(guides["overview"].body.strip())]
    for name in sorted(n for n in guides if n != "overview"):
        heading = f"## Guide: {name}\n\n{guides[name].description}"
        assert heading in stable, name
        positions.append(stable.index(heading))
    assert positions == sorted(positions)
    assert "## Guide: overview" not in stable

    assert profile.startswith("## The user's profile")
    assert "my game" in profile  # the seeded charter
    assert '"chores"' in profile and '"energy"' in profile
    assert '"now"' not in profile  # the time lives in the user messages only


def test_the_profile_is_as_it_was_when_the_session_started(
    orchard: Orchard, clock: FixedClock
) -> None:
    orchard.write_profile_note("energy", "Low after 3pm.")
    session = start_session(orchard, clock, models(text("ok")))
    orchard.write_profile_note("energy", "High all day.")
    run_turn(session, "hello")
    assert "Low after 3pm." in session.system[1]["text"]
    assert "High all day." not in session.system[1]["text"]


def test_the_stable_block_does_not_depend_on_the_profile(
    orchard: Orchard, clock: FixedClock
) -> None:
    first = start_session(orchard, clock, models())
    orchard.write_profile_note("charter", "Keep evenings free.")
    clock.advance(days=3)
    second = start_session(orchard, clock, models())
    assert first.system[0] == second.system[0]
    assert first.system[1] != second.system[1]


def test_the_timezone_warning_only_when_not_detected(orchard: Orchard, clock: FixedClock) -> None:
    quiet = start_session(orchard, clock, models())
    loud = start_session(orchard, clock, models(), timezone_detected=False)
    assert TIMEZONE_WARNING not in quiet.system[0]["text"]
    assert TIMEZONE_WARNING in loud.system[0]["text"]
    orchard.set_timezone("Europe/London")  # confirmed with the user: no longer a guess
    settled = start_session(orchard, clock, models(), timezone_detected=False)
    assert TIMEZONE_WARNING not in settled.system[0]["text"]


def test_every_user_message_starts_with_the_time(orchard: Orchard, clock: FixedClock) -> None:
    scripted = models(text("one"), text("two"))
    session = start_session(orchard, clock, scripted)
    run_turn(session, "first")
    clock.advance(hours=7, minutes=30)
    run_turn(session, "second")
    typed = [m for m in session.messages if m["role"] == "user"]
    assert typed[0]["content"][0]["text"] == "[Now: Wed 2026-03-18T10:00, Europe/London]\nfirst"
    assert typed[1]["content"][0]["text"] == "[Now: Wed 2026-03-18T17:30, Europe/London]\nsecond"


def test_now_line_uses_local_time() -> None:
    summer = datetime(2026, 7, 3, 16, 5, tzinfo=UTC)
    assert (
        now_line(summer, ZoneInfo("Europe/London")) == "[Now: Fri 2026-07-03T17:05, Europe/London]"
    )


def test_system_prompt_has_two_text_blocks() -> None:
    blocks = system_prompt(load_guides(), {"notes": []}, timezone_detected=True)
    assert [b["type"] for b in blocks] == ["text", "text"]
    assert all("cache_control" not in b for b in blocks)  # caching is model.py's business
