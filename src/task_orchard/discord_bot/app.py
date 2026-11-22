"""What the Discord bot does, with no Discord in it, so it can be tested offline.

`BotApp` turns a direct message into the messages to send back, and a slash
command into its output. It runs on the bot's one worker thread (client.py);
nothing here is called from anywhere else.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from task_orchard.agent import commands
from task_orchard.agent.chat import LONG_SESSION
from task_orchard.agent.continuity import DEFAULT_IDLE, Conversation
from task_orchard.agent.model import ModelUnavailable
from task_orchard.config import (
    ENV_DISCORD_IDLE_MINUTES,
    ENV_DISCORD_TOKEN,
    ENV_DISCORD_USER_ID,
    Settings,
)

LIMIT = 2000  # characters in one Discord message
NEW_CONVERSATION = "-# New conversation."
NO_ANSWER = "(The model gave no answer.)"
NOT_OWNER = "This bot answers only the person who runs it."
DONE = "Done."


@dataclass(frozen=True)
class BotSettings:
    token: str
    owner_id: int
    idle: timedelta


def bot_settings(settings: Settings) -> BotSettings | str:
    """The bot's settings, or what is wrong with them, to print before exiting 1."""
    problems: list[str] = []
    if not settings.discord_token:
        problems.append(
            f"{ENV_DISCORD_TOKEN} is not set: give it the bot's token from the Discord "
            "developer portal."
        )
    owner_id = 0
    raw_user = (settings.discord_user_id or "").strip()
    if not raw_user:
        problems.append(
            f"{ENV_DISCORD_USER_ID} is not set: give it your Discord user id (Developer "
            "Mode, then right-click your name and Copy User ID)."
        )
    elif not raw_user.isdigit():
        problems.append(f"{ENV_DISCORD_USER_ID} must be a Discord user id, digits only.")
    else:
        owner_id = int(raw_user)
    idle = DEFAULT_IDLE
    raw_idle = (settings.discord_idle_minutes or "").strip()
    if raw_idle:
        if raw_idle.isdigit() and int(raw_idle) > 0:
            idle = timedelta(minutes=int(raw_idle))
        else:
            problems.append(f"{ENV_DISCORD_IDLE_MINUTES} must be a whole number of minutes.")
    if problems:
        return "\n".join(problems)
    return BotSettings(settings.discord_token or "", owner_id, idle)


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Pieces of at most `limit` characters, cut at a paragraph break, a line break or a
    space where there is one in the second half of the piece, otherwise anywhere."""
    pieces: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        window = rest[: limit + 1]
        cut = -1
        for mark in ("\n\n", "\n", " "):
            found = window.rfind(mark, 0, limit)
            if found >= limit // 2:
                cut = found
                break
        if cut < 0:
            cut = limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


@dataclass(frozen=True)
class Output:
    """A command's output: in a code block when it fits in one message, else a file."""

    text: str | None = None
    filename: str | None = None
    data: bytes | None = None


def command_output(name: str, shown: str) -> Output:
    shown = shown.strip("\n")
    if not shown.strip():
        return Output(text=DONE)
    block = "```\n" + shown.replace("```", "`​``") + "\n```"
    if len(block) <= LIMIT:
        return Output(text=block)
    return Output(filename=f"{name}.txt", data=(shown + "\n").encode("utf-8"))


class BotApp:
    def __init__(self, conversation: Conversation) -> None:
        self.conversation = conversation
        # The ids of the messages the bot sent, with the number of the session each
        # belongs to: a Discord reply to one continues that session if it is current.
        self.sent: dict[int, int] = {}

    def answer(self, text: str, reply_to: int | None = None) -> list[str]:
        """The messages that answer `text`, a direct message from the owner."""
        trace = self.conversation.trace
        replying = (
            reply_to is not None
            and self.conversation.started
            and self.sent.get(reply_to) == trace.session_number
        )
        try:
            turn = self.conversation.turn(text, replying=replying)
        except ModelUnavailable as err:
            return [f"Could not reach the model: {err}"]
        body = turn.answer.strip() or NO_ANSWER
        if turn.long:
            body += f"\n\n{LONG_SESSION}"
        if turn.new_conversation:
            body += f"\n{NEW_CONVERSATION}"
        return split_message(body)

    def record_sent(self, message_ids: list[int]) -> None:
        """Remember the bot's messages that answered the latest turn."""
        for message_id in message_ids:
            self.sent[message_id] = self.conversation.trace.session_number

    def command(self, name: str, args: Mapping[str, str]) -> Output:
        command = commands.find(name)
        if command is None or command.terminal_only:
            return Output(text=f"There is no command /{name}.")
        shown = io.StringIO()
        command.run(self.conversation, dict(args), shown, shown)
        return command_output(command.name, shown.getvalue())

    def complete(self, name: str, param: str, typed: str) -> list[str]:
        """Completions for a command's parameter, those containing `typed` first."""
        command = commands.find(name)
        found = next((p for p in command.params if p.name == param), None) if command else None
        if found is None or found.complete is None:
            return []
        values = found.complete(self.conversation)
        typed = typed.lower()
        return [v for v in values if typed in v.lower()]
