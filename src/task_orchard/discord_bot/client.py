"""The Discord side of the bot (SPEC 10.5). The only module that imports discord.

All the bot's work runs on one worker thread, one job at a time: opening the
Orchard, every turn and every command. Nothing reaches SQLite from another
thread, and a command sent during a turn waits for it. Completions are the one
exception: see _completer. The event
loop only talks to Discord.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

import discord
from discord import app_commands

from task_orchard.agent import commands
from task_orchard.agent.chat import credentials_problem, models_for
from task_orchard.agent.commands import Command
from task_orchard.agent.continuity import Conversation
from task_orchard.agent.model import has_credentials
from task_orchard.agent.trace import Trace
from task_orchard.config import Settings
from task_orchard.core.clock import SystemClock
from task_orchard.core.service import Orchard
from task_orchard.discord_bot.app import NOT_OWNER, BotApp, BotSettings, bot_settings

T = TypeVar("T")

# Offered in DMs with the bot and in servers, for a bot installed either way.
CONTEXTS = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)
INSTALLS = app_commands.AppInstallationType(guild=True, user=True)
MAX_CHOICES = 25  # Discord's limit on completions


class OrchardBot(discord.Client):
    def __init__(self, owner_id: int, build_app: Callable[[], BotApp]) -> None:
        # The default intents include direct messages. Their content arrives without
        # the privileged message-content intent, which only server messages need.
        super().__init__(intents=discord.Intents.default())
        self.owner_id = owner_id
        self._build_app = build_app
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="orchard")
        self._app: BotApp | None = None
        self.tree = build_tree(self)

    @property
    def app(self) -> BotApp:
        assert self._app is not None, "setup_hook builds the app before any event"
        return self._app

    async def work(self, fn: Callable[..., T], *args: Any) -> T:
        """Run `fn(*args)` on the worker thread."""
        return await asyncio.get_running_loop().run_in_executor(self.worker, partial(fn, *args))

    async def setup_hook(self) -> None:
        self._app = await self.work(self._build_app)
        await self.tree.sync()

    async def on_ready(self) -> None:
        print(f"Connected to Discord as {self.user}. Ctrl-C stops the bot.", file=sys.stderr)

    async def close(self) -> None:
        await super().close()
        self.worker.shutdown(wait=True)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.author.id != self.owner_id:
            return
        if message.guild is not None:  # direct messages only
            return
        text = message.content.strip()
        if not text:
            return
        reply_to = message.reference.message_id if message.reference else None
        async with message.channel.typing():
            pieces = await self.work(self.app.answer, text, reply_to)
        sent = [(await message.channel.send(piece)).id for piece in pieces]
        await self.work(self.app.record_sent, sent)


def build_tree(bot: OrchardBot) -> app_commands.CommandTree:
    """Every command of the registry that is not terminal-only, as a slash command."""
    tree = app_commands.CommandTree(bot)
    for command in commands.COMMANDS:
        if not command.terminal_only:
            tree.add_command(slash_command(bot, command))
    return tree


def slash_command(bot: OrchardBot, command: Command) -> app_commands.Command:
    async def callback(interaction: discord.Interaction, **values: str | None) -> None:
        if interaction.user.id != bot.owner_id:
            await interaction.response.send_message(NOT_OWNER, ephemeral=True)
            return
        # Acknowledge first: Discord drops an interaction not answered within three
        # seconds, and the worker may be busy with a turn.
        await interaction.response.defer(thinking=True)
        args = {name: value for name, value in values.items() if value is not None}
        output = await bot.work(bot.app.command, command.name, args)
        if output.text is not None:
            await interaction.followup.send(output.text)
        else:
            assert output.data is not None and output.filename is not None
            attached = discord.File(io.BytesIO(output.data), filename=output.filename)
            await interaction.followup.send(file=attached)

    # discord.py reads a command's options from the callback's signature, so the
    # registry's parameters become a signature here.
    parameters = [
        inspect.Parameter(
            "interaction", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=discord.Interaction
        )
    ]
    for param in command.params:
        parameters.append(
            inspect.Parameter(
                param.name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=str if param.required else str | None,
                default=inspect.Parameter.empty if param.required else None,
            )
        )
    callback.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    callback.__discord_app_commands_param_description__ = {  # type: ignore[attr-defined]
        param.name: param.description for param in command.params
    }
    slash = app_commands.Command(
        name=command.name,
        description=command.summary,
        callback=callback,
        allowed_contexts=CONTEXTS,
        allowed_installs=INSTALLS,
    )
    for param in command.params:
        if param.complete is not None:
            slash.autocomplete(param.name)(_completer(bot, command.name, param.name))
    return slash


def _completer(bot: OrchardBot, name: str, param: str) -> Callable[..., Any]:
    async def complete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.user.id != bot.owner_id:
            return []
        # Discord gives a completion three seconds and it cannot be deferred, so it
        # does not queue behind a turn. Once a session exists the completions come
        # from its fixed tool list, with no database access; before then, the worker
        # starts the session.
        if bot.app.conversation.started:
            values = bot.app.complete(name, param, current)
        else:
            values = await bot.work(bot.app.complete, name, param, current)
        return [app_commands.Choice(name=v, value=v) for v in values[:MAX_CHOICES]]

    return complete


def main(
    settings: Settings,
    *,
    transcript: Path | None = None,
    keep_transcript: bool = True,
    credentials: Callable[[], bool] | None = None,
) -> int:
    """`task-orchard discord`: checks every setting, then runs until stopped."""
    checked = bot_settings(settings)
    if isinstance(checked, str):
        print(checked, file=sys.stderr)
        return 1
    problem = credentials_problem(settings, credentials or has_credentials)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    clock = SystemClock()
    if keep_transcript and transcript is None:
        transcript = settings.transcript_dir / clock.now().strftime("discord-%Y%m%d-%H%M%SZ.jsonl")
    log = None
    if keep_transcript and transcript is not None:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        log = transcript.open("a", encoding="utf-8")
        print(f"Transcript: {transcript}", file=sys.stderr, flush=True)
    try:
        bot = OrchardBot(checked.owner_id, partial(_build_app, settings, checked, Trace(log)))
        # discord.py logs its own connection events to stderr.
        bot.run(checked.token)
    finally:
        if log is not None:
            log.close()
    return 0


def _build_app(settings: Settings, checked: BotSettings, trace: Trace) -> BotApp:
    """Runs on the worker thread, so the Orchard's connections are made there."""
    clock = SystemClock()
    orchard = Orchard.open(settings, clock)
    conversation = Conversation(
        orchard,
        clock,
        models_for(settings),
        trace,
        idle=checked.idle,
        timezone_detected=settings.timezone_detected,
    )
    return BotApp(conversation)
