"""SPEC 10.5: one command registry for chat and the Discord bot."""

from __future__ import annotations

import io

import pytest
from discord import app_commands

from task_orchard.agent import commands
from task_orchard.agent.chat import chat
from task_orchard.agent.commands import COMMANDS, Command, Param, find, help_text, parse_args
from task_orchard.agent.model import BASIC, STRATEGIC
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from task_orchard.discord_bot.client import OrchardBot
from tests.scripted import ScriptedModel


def _run_chat(orchard: Orchard, clock: FixedClock, lines: list[str]) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    models = {BASIC: ScriptedModel([]), STRATEGIC: ScriptedModel([])}
    chat(
        orchard,
        clock,
        models=models,
        stdin=io.StringIO("".join(f"{line}\n" for line in lines)),
        stdout=out,
        stderr=err,
    )
    assert models[BASIC].requests == []
    return out.getvalue(), err.getvalue()


def _echo(host: commands.Host, args: commands.Args, out, err) -> None:  # noqa: ANN001
    print(f"echo {dict(args)}", file=out)


ECHO = Command(
    "echo",
    "repeat what it was given",
    _echo,
    params=(Param("word", "one word", True), Param("rest", "the rest")),
    testing=True,
)


def _tree() -> app_commands.CommandTree:
    return OrchardBot(1, lambda: None).tree  # type: ignore[arg-type, return-value]


def test_help_is_built_from_the_registry() -> None:
    terminal = help_text(terminal=True)
    for command in COMMANDS:
        for name in (command.name, *command.aliases):
            assert f"/{name}" in terminal
    elsewhere = help_text(terminal=False)
    assert "/exit" not in elsewhere and "/quit" not in elsewhere and "/usage" in elsewhere


def test_summaries_and_descriptions_fit_discord() -> None:
    for command in COMMANDS:
        assert len(command.summary) <= 100, command.name
        for param in command.params:
            assert len(param.description) <= 100, (command.name, param.name)


def test_parse_args_gives_the_last_parameter_the_rest_of_the_line() -> None:
    tool = find("/tool")
    assert tool is not None
    assert parse_args(tool, ' get_task {"task_id": 12}') == {
        "name": "get_task",
        "arguments": '{"task_id": 12}',
    }
    assert parse_args(tool, "read_profile") == {"name": "read_profile"}
    assert parse_args(tool, "") == {}
    assert find("quit") is find("exit") and find("nonsense") is None


def test_every_command_but_the_terminal_ones_is_a_slash_command() -> None:
    tree = _tree()
    names = {c.name for c in tree.get_commands()}
    assert names == {c.name for c in COMMANDS if not c.terminal_only}
    tool = tree.get_command("tool")
    assert isinstance(tool, app_commands.Command)
    options = {p.name: p for p in tool.parameters}
    assert options["name"].required and options["name"].autocomplete
    assert not options["arguments"].required
    shown = tool.to_dict(tree)
    assert shown["contexts"] == [0, 1, 2]  # servers, DMs with the bot, private channels


def test_a_command_added_to_the_registry_appears_in_chat_and_discord(
    orchard: Orchard, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "COMMANDS", (*COMMANDS, ECHO))
    out, err = _run_chat(orchard, clock, ["/help", "/echo hello there world"])
    assert "/echo WORD [REST]" in err
    assert "echo {'word': 'hello', 'rest': 'there world'}" in out
    echo = _tree().get_command("echo")
    assert isinstance(echo, app_commands.Command)
    assert [(p.name, p.required, p.description) for p in echo.parameters] == [
        ("word", True, "one word"),
        ("rest", False, "the rest"),
    ]
