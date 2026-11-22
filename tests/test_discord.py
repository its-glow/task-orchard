"""SPEC 10.5: the Discord bot, without Discord. A scripted model; no network."""

from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from task_orchard import cli
from task_orchard.agent.chat import LONG_SESSION
from task_orchard.agent.continuity import (
    CONTINUE,
    NEW,
    Conversation,
    ago,
    continuity_prompt,
    decision,
)
from task_orchard.agent.loop import SESSION_TOKEN_LIMIT
from task_orchard.agent.model import BASIC, STRATEGIC, ModelUnavailable, Reply, Usage
from task_orchard.agent.trace import Trace
from task_orchard.config import load_settings
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from task_orchard.discord_bot import client
from task_orchard.discord_bot.app import (
    DONE,
    LIMIT,
    NEW_CONVERSATION,
    NOT_OWNER,
    BotApp,
    BotSettings,
    bot_settings,
    split_message,
)
from tests.scripted import ScriptedModel, calls, text

IDLE = timedelta(minutes=30)


class Bot:
    """A BotApp on scripted models, with its transcript in memory."""

    def __init__(self, orchard: Orchard, clock: FixedClock, basic=(), deep=()) -> None:  # noqa: ANN001
        self.models = {
            BASIC: ScriptedModel(list(basic), "small-1"),
            STRATEGIC: ScriptedModel(list(deep), "big-1"),
        }
        self.log = io.StringIO()
        self.conversation = Conversation(orchard, clock, self.models, Trace(self.log), idle=IDLE)
        self.app = BotApp(self.conversation)
        self.next_id = 100

    def say(self, words: str, reply_to: int | None = None) -> list[str]:
        pieces = self.app.answer(words, reply_to)
        ids = list(range(self.next_id, self.next_id + len(pieces)))
        self.next_id += len(pieces)
        self.app.record_sent(ids)
        return pieces

    @property
    def last_sent(self) -> int:
        return self.next_id - 1

    @property
    def session_number(self) -> int:
        return self.conversation.trace.session_number

    def records(self, kind: str) -> list[dict]:
        lines = [json.loads(line) for line in self.log.getvalue().splitlines()]
        return [r for r in lines if r["type"] == kind]


def _sent_texts(model: ScriptedModel, request: int) -> list[str]:
    """The user texts of one request, without the [Now: ...] line."""
    texts = []
    for message in model.requests[request].messages:
        for block in message["content"]:
            if message["role"] == "user" and block.get("type") == "text":
                texts.append(block["text"].split("\n", 1)[-1])
    return texts


# --- which session a message goes to ---


def test_the_first_message_starts_a_session(orchard: Orchard, clock: FixedClock) -> None:
    bot = Bot(orchard, clock, basic=[text("Hello.")])
    assert bot.say("Hi") == ["Hello."]
    assert bot.session_number == 1
    assert bot.records("continuity") == []


def test_within_the_idle_limit_the_session_continues_unchecked(
    orchard: Orchard, clock: FixedClock
) -> None:
    bot = Bot(orchard, clock, basic=[text("Snooze it?"), text("Snoozed.")])
    bot.say("Do the taxes")
    clock.advance(minutes=29)
    assert bot.say("Yes") == ["Snoozed."]
    assert bot.session_number == 1
    assert _sent_texts(bot.models[BASIC], 1) == ["Do the taxes", "Yes"]
    assert bot.records("continuity") == []


def test_after_the_idle_limit_the_check_can_continue_the_session(
    orchard: Orchard, clock: FixedClock
) -> None:
    bot = Bot(orchard, clock, basic=[text("Snooze it?"), text("continue"), text("Snoozed.")])
    bot.say("Do the taxes")
    clock.advance(hours=2, minutes=5)
    assert bot.say("yes please") == ["Snoozed."]
    assert bot.session_number == 1
    assert _sent_texts(bot.models[BASIC], 2) == ["Do the taxes", "yes please"]


def test_after_the_idle_limit_the_check_can_start_a_new_session(
    orchard: Orchard, clock: FixedClock
) -> None:
    bot = Bot(orchard, clock, basic=[text("Done."), text("new"), text("Try the bike light.")])
    bot.say("Mark the taxes done")
    clock.advance(hours=3)
    assert bot.say("What now?") == [f"Try the bike light.\n{NEW_CONVERSATION}"]
    assert bot.session_number == 2
    assert _sent_texts(bot.models[BASIC], 2) == ["What now?"]


def test_a_reply_to_the_bot_continues_its_session_without_a_check(
    orchard: Orchard, clock: FixedClock
) -> None:
    bot = Bot(orchard, clock, basic=[text("Which one?"), text("The first.")])
    bot.say("Pick a task")
    clock.advance(days=2)
    assert bot.say("the first", reply_to=bot.last_sent) == ["The first."]
    assert bot.session_number == 1
    assert bot.records("continuity") == []


def test_a_reply_to_an_earlier_session_is_not_a_reply(orchard: Orchard, clock: FixedClock) -> None:
    bot = Bot(orchard, clock, basic=[text("Old."), text("New one."), text("new"), text("Fresh.")])
    bot.say("first")
    old = bot.last_sent
    bot.app.command("new", {})
    bot.say("second")
    clock.advance(hours=1)
    assert bot.say("about that", reply_to=old) == [f"Fresh.\n{NEW_CONVERSATION}"]
    assert len(bot.records("continuity")) == 1


def test_new_starts_a_fresh_session(orchard: Orchard, clock: FixedClock) -> None:
    bot = Bot(orchard, clock, basic=[text("One."), text("Two.")])
    bot.say("first")
    assert bot.app.command("new", {}).text == "```\n[basic: small-1]\n```"
    assert bot.say("second") == ["Two."]
    assert bot.session_number == 2
    assert _sent_texts(bot.models[BASIC], 1) == ["second"]


# --- the continuity check ---


def test_the_check_request(orchard: Orchard, clock: FixedClock) -> None:
    bot = Bot(orchard, clock, basic=[text("Shall I snooze it?"), text("new"), text("Hi.")])
    bot.say("Taxes")
    clock.advance(hours=2, minutes=5)
    bot.say("Hello")
    request = bot.models[BASIC].requests[1]
    assert request.tools == []
    assert request.system == [{"type": "text", "text": continuity_prompt()}]
    [message] = request.messages
    sent = message["content"][0]["text"]
    assert "Shall I snooze it?" in sent and "Hello" in sent
    assert "2 hours 5 minutes before the new message" in sent


def test_the_check_runs_on_the_basic_tier(orchard: Orchard, clock: FixedClock) -> None:
    bot = Bot(orchard, clock, basic=[text("continue")], deep=[text("Hmm?"), text("Ok.")])
    bot.app.command("strategic", {})
    bot.say("Plan my week")
    clock.advance(hours=1)
    assert bot.say("go on") == ["Ok."]
    assert len(bot.models[BASIC].requests) == 1


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("continue", CONTINUE),
        ("Continue.", CONTINUE),
        ("`continue`", CONTINUE),
        ("new", NEW),
        ("I think it continues", NEW),
        ("", NEW),
    ],
)
def test_reading_the_check(reply: str, expected: str) -> None:
    assert decision(text(reply)) == expected


def test_the_check_goes_to_the_transcript(orchard: Orchard, clock: FixedClock) -> None:
    checked = Reply(content=[{"type": "text", "text": "new"}], stop="end", usage=Usage(80, 0, 0, 1))
    bot = Bot(orchard, clock, basic=[text("Hi."), checked, text("Hello again.")])
    bot.say("Hi")
    clock.advance(hours=1)
    bot.say("Hi again")
    assert bot.records("continuity") == [
        {
            "session": 1,
            "type": "continuity",
            "decision": "new",
            "reply": "new",
            "model": "small-1",
            "input": 80,
            "cache_write": 0,
            "cache_read": 0,
            "output": 1,
        }
    ]
    assert {r["session"] for r in bot.records("message")} == {1, 2}


def test_a_failed_check_is_reported_and_changes_nothing(
    orchard: Orchard, clock: FixedClock
) -> None:
    bot = Bot(orchard, clock, basic=[text("Hi."), ModelUnavailable("offline"), text("continue")])
    bot.say("Hi")
    clock.advance(hours=1)
    assert bot.say("Still there?") == ["Could not reach the model: offline"]
    assert bot.session_number == 1
    assert len(bot.conversation.session.messages) == 2


def test_ago() -> None:
    assert ago(timedelta(minutes=40)) == "40 minutes"
    assert ago(timedelta(hours=1, minutes=1)) == "1 hour 1 minute"
    assert ago(timedelta(days=2, hours=3, minutes=9)) == "2 days 3 hours"
    assert ago(timedelta(seconds=20)) == "0 minutes"


# --- answers ---


def test_a_long_answer_is_split(orchard: Orchard, clock: FixedClock) -> None:
    paragraph = "word " * 300  # 1,500 characters
    bot = Bot(orchard, clock, basic=[text(f"{paragraph}\n\n{paragraph}\n\n{paragraph}")])
    pieces = bot.say("Tell me everything")
    assert len(pieces) == 3
    assert all(len(p) <= LIMIT for p in pieces)


def test_split_message() -> None:
    assert split_message("short") == ["short"]
    lines = "\n".join(["x" * 100] * 30)
    assert all(len(p) <= LIMIT and not p.startswith("\n") for p in split_message(lines))
    solid = "y" * (LIMIT * 2 + 5)
    assert split_message(solid) == ["y" * LIMIT, "y" * LIMIT, "y" * 5]
    assert "".join(split_message(solid)) == solid


def test_the_long_session_note_comes_once(orchard: Orchard, clock: FixedClock) -> None:
    big = SESSION_TOKEN_LIMIT + 1
    bot = Bot(orchard, clock, basic=[text("One.", tokens=big), text("Two.", tokens=big)])
    assert bot.say("a") == [f"One.\n\n{LONG_SESSION}"]
    assert bot.say("b") == ["Two."]


def test_tool_calls_are_not_shown_but_last_has_them(orchard: Orchard, clock: FixedClock) -> None:
    bot = Bot(orchard, clock, basic=[calls(("read_profile", {})), text("Here.")])
    assert bot.say("Profile?") == ["Here."]
    shown = bot.app.command("last", {})
    assert shown.text is not None and "-> read_profile()" in shown.text
    assert len(bot.records("tool")) == 1


def test_an_unreachable_model_is_reported(orchard: Orchard, clock: FixedClock) -> None:
    bot = Bot(orchard, clock, basic=[ModelUnavailable("no route to host")])
    assert bot.say("Hi") == ["Could not reach the model: no route to host"]


def test_an_empty_answer_still_sends_something(orchard: Orchard, clock: FixedClock) -> None:
    bot = Bot(orchard, clock, basic=[text("")])
    assert bot.say("Hi") == ["(The model gave no answer.)"]


# --- commands ---


def test_command_output(orchard: Orchard, clock: FixedClock, seeded) -> None:  # noqa: ANN001
    bot = Bot(orchard, clock)
    small = bot.app.command(
        "tool", {"name": "get_task", "arguments": f'{{"task_id": {seeded["milk"]}}}'}
    )
    assert small.text is not None and small.text.startswith("```\n{")
    assert "Buy milk" in small.text
    big = bot.app.command("prompt", {})
    assert big.text is None and big.filename == "prompt.txt"
    assert big.data is not None and b"## Guide: triage" in big.data
    assert bot.app.command("basic", {}).text == "```\n[basic: small-1]\n```"
    complaint = bot.app.command("tool", {"name": "drop_tasks"})
    assert complaint.text is not None and "read-only" in complaint.text
    assert bot.app.command("exit", {}).text == "There is no command /exit."
    assert bot.models[BASIC].requests == []


def test_a_command_that_shows_nothing(orchard: Orchard, clock: FixedClock, monkeypatch) -> None:  # noqa: ANN001
    from task_orchard.agent import commands

    quiet = commands.Command("quiet", "says nothing", lambda host, args, out, err: None)
    monkeypatch.setattr(commands, "COMMANDS", (*commands.COMMANDS, quiet))
    assert Bot(orchard, clock).app.command("quiet", {}).text == DONE


def test_completion_offers_read_only_tools(orchard: Orchard, clock: FixedClock) -> None:
    app = Bot(orchard, clock).app
    offered = app.complete("tool", "name", "CAND")
    assert offered == ["get_candidates"]
    assert "drop_tasks" not in app.complete("tool", "name", "")
    assert app.complete("tool", "arguments", "") == []


# --- the owner check, on the Discord side ---

OWNER = 42


def _offline_bot() -> tuple[client.OrchardBot, Mock]:
    bot = client.OrchardBot(OWNER, lambda: None)  # type: ignore[arg-type, return-value]
    app = Mock()
    bot._app = app
    return bot, app


def _message(author: int, *, is_bot: bool = False, guild: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        author=SimpleNamespace(id=author, bot=is_bot),
        guild=guild,
        content="What now?",
        reference=None,
        channel=Mock(),
    )


@pytest.mark.parametrize(
    "message",
    [
        _message(7),  # someone else
        _message(OWNER, is_bot=True),  # a bot, the bot itself included
        _message(OWNER, guild=object()),  # the owner, but in a server channel
    ],
    ids=["stranger", "bot", "server"],
)
async def test_messages_reach_the_app_only_from_the_owner_in_dms(message) -> None:  # noqa: ANN001
    bot, app = _offline_bot()
    await bot.on_message(message)
    assert app.mock_calls == []
    assert message.channel.mock_calls == []


def _interaction(user: int) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def test_commands_from_anyone_else_are_refused_privately() -> None:
    bot, app = _offline_bot()
    stranger = _interaction(7)
    await bot.tree.get_command("usage").callback(stranger)  # type: ignore[union-attr]
    stranger.response.send_message.assert_awaited_once_with(NOT_OWNER, ephemeral=True)
    stranger.response.defer.assert_not_awaited()
    assert app.mock_calls == []


async def test_the_owners_command_is_acknowledged_first() -> None:
    from task_orchard.discord_bot.app import Output

    bot, app = _offline_bot()
    app.command.return_value = Output(text="```\nhi\n```")
    owner = _interaction(OWNER)
    await bot.tree.get_command("tool").callback(owner, name="read_profile", arguments=None)  # type: ignore[union-attr]
    owner.response.defer.assert_awaited_once()
    app.command.assert_called_once_with("tool", {"name": "read_profile"})
    owner.followup.send.assert_awaited_once_with("```\nhi\n```")
    bot.worker.shutdown()


# --- settings and the command line ---


def _settings(**env: str):  # noqa: ANN202
    return load_settings({"TASK_ORCHARD_DB": "/tmp/unused.db", **env})


def test_bot_settings() -> None:
    checked = bot_settings(
        _settings(
            TASK_ORCHARD_DISCORD_TOKEN="t0k",
            TASK_ORCHARD_DISCORD_USER_ID="1234",
            TASK_ORCHARD_DISCORD_IDLE_MINUTES="45",
        )
    )
    assert checked == BotSettings("t0k", 1234, timedelta(minutes=45))
    default = bot_settings(
        _settings(TASK_ORCHARD_DISCORD_TOKEN="t", TASK_ORCHARD_DISCORD_USER_ID="1")
    )
    assert isinstance(default, BotSettings) and default.idle == IDLE
    missing = bot_settings(_settings())
    assert isinstance(missing, str)
    assert "TASK_ORCHARD_DISCORD_TOKEN" in missing and "TASK_ORCHARD_DISCORD_USER_ID" in missing
    wrong = bot_settings(
        _settings(
            TASK_ORCHARD_DISCORD_TOKEN="t",
            TASK_ORCHARD_DISCORD_USER_ID="@me",
            TASK_ORCHARD_DISCORD_IDLE_MINUTES="soon",
        )
    )
    assert isinstance(wrong, str) and "digits" in wrong and "whole number" in wrong


def test_the_token_is_never_shown() -> None:
    assert "t0k" not in repr(_settings(TASK_ORCHARD_DISCORD_TOKEN="t0k"))


def test_discord_exits_1_on_missing_settings_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.delenv("TASK_ORCHARD_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("TASK_ORCHARD_DISCORD_USER_ID", raising=False)
    monkeypatch.setenv("TASK_ORCHARD_DB", str(tmp_path / "never.db"))
    assert cli.main(["discord"]) == 1
    assert "TASK_ORCHARD_DISCORD_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "never.db").exists()


def test_discord_exits_1_without_model_credentials(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    settings = _settings(TASK_ORCHARD_DISCORD_TOKEN="t", TASK_ORCHARD_DISCORD_USER_ID="1")
    assert client.main(settings, credentials=lambda: False) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
