"""`task-orchard chat` and `task-orchard ask` (SPEC 10.2).

The answer goes to stdout. Everything else (the tier in use, one line per tool
call, notices and errors) goes to stderr, so the user can watch what the model
does and still pipe the answers.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from task_orchard.agent.loop import SESSION_TOKEN_LIMIT, Event, Session, run_turn, start_session
from task_orchard.agent.model import (
    BASIC,
    EFFORT,
    STRATEGIC,
    AnthropicModel,
    Model,
    ModelUnavailable,
    has_credentials,
)
from task_orchard.config import Settings
from task_orchard.core.clock import Clock
from task_orchard.core.service import Orchard

CREDENTIALS_HELP = (
    "Task Orchard's assistant needs credentials for the Claude API.\n"
    "Set ANTHROPIC_API_KEY, or log in with the ant CLI (ant auth login), then try again.\n"
    "Model calls are billed to that account."
)
COMMANDS_HELP = "/strategic and /basic switch tier, /new starts again, /quit leaves."
LONG_SESSION = (
    "This conversation is getting long. /new starts a fresh one; your tasks and profile are kept."
)


def models_for(settings: Settings) -> dict[str, Model]:
    return {
        BASIC: AnthropicModel(settings.model_basic, EFFORT[BASIC]),
        STRATEGIC: AnthropicModel(settings.model_strategic, EFFORT[STRATEGIC]),
    }


def _model_name(model: Model) -> str:
    return str(getattr(model, "model_id", type(model).__name__))


def tool_line(event: Event) -> str:
    """-> get_candidates(minutes_available=30, energy="low"), then !! code on failure."""
    arguments = event.get("input")
    if isinstance(arguments, dict):
        shown = ", ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in arguments.items()
        )
    else:
        shown = json.dumps(arguments, ensure_ascii=False)
    line = f"-> {event['name']}({shown})"
    if event.get("is_error"):
        line += f" !! {event['payload'].get('error', 'error')}"
    return line


class _Trace:
    """Reports tool calls on stderr and writes the transcript, if there is one."""

    def __init__(self, stderr: TextIO, transcript: TextIO | None) -> None:
        self.stderr = stderr
        self.transcript = transcript
        self.session_number = 0

    def __call__(self, event: Event) -> None:
        if event["type"] == "tool":
            print(tool_line(event), file=self.stderr, flush=True)
        if self.transcript is not None:
            record = {"session": self.session_number, **event}
            self.transcript.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.transcript.flush()


def _announce(session: Session, stderr: TextIO) -> None:
    print(f"[{session.tier}: {_model_name(session.model)}]", file=stderr, flush=True)


def chat(
    orchard: Orchard,
    clock: Clock,
    *,
    models: Mapping[str, Model],
    timezone_detected: bool = True,
    strategic: bool = False,
    transcript: Path | None = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """A terminal conversation. Each line typed is a turn."""
    log = transcript.open("a", encoding="utf-8") if transcript is not None else None
    trace = _Trace(stderr, log)

    def new_session(tier: str) -> Session:
        trace.session_number += 1
        session = start_session(
            orchard, clock, models, tier=tier, timezone_detected=timezone_detected, on_event=trace
        )
        _announce(session, stderr)
        return session

    try:
        print(f"Task Orchard. {COMMANDS_HELP}", file=stderr, flush=True)
        session = new_session(STRATEGIC if strategic else BASIC)
        warned = False
        interactive = stdin.isatty()
        while True:
            if interactive:
                print("> ", end="", file=stderr, flush=True)
            line = stdin.readline()
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            if text == "/quit":
                break
            if text == "/new":
                session = new_session(BASIC)
                warned = False
                continue
            if text in ("/strategic", "/basic"):
                session.switch(text[1:])
                _announce(session, stderr)
                continue
            if text.startswith("/"):
                print(f"Unknown command. {COMMANDS_HELP}", file=stderr, flush=True)
                continue
            try:
                answer = run_turn(session, text)
            except ModelUnavailable as err:
                print(f"Could not reach the model: {err}", file=stderr, flush=True)
                continue
            print(answer, file=stdout, flush=True)
            if session.input_tokens > SESSION_TOKEN_LIMIT and not warned:
                print(LONG_SESSION, file=stderr, flush=True)
                warned = True
        return 0
    finally:
        if log is not None:
            log.close()


def ask(
    orchard: Orchard,
    clock: Clock,
    text: str,
    *,
    models: Mapping[str, Model],
    timezone_detected: bool = True,
    strategic: bool = False,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """One turn in a new session. Exit 0 with the answer, 1 if the model could not be reached."""
    trace = _Trace(stderr, None)
    session = start_session(
        orchard,
        clock,
        models,
        tier=STRATEGIC if strategic else BASIC,
        timezone_detected=timezone_detected,
        on_event=trace,
    )
    try:
        answer = run_turn(session, text)
    except ModelUnavailable as err:
        print(f"Could not reach the model: {err}", file=stderr, flush=True)
        return 1
    print(answer, file=stdout, flush=True)
    return 0


def main(
    command: str,
    settings: Settings,
    open_orchard: Callable[[], tuple[Orchard, Clock]],
    *,
    text: str | None = None,
    strategic: bool = False,
    transcript: Path | None = None,
    credentials: Callable[[], bool] | None = None,
) -> int:
    """The entry point cli uses. Checks credentials before anything is read."""
    if not (credentials or has_credentials)():
        print(CREDENTIALS_HELP, file=sys.stderr)
        return 1
    orchard, clock = open_orchard()
    models = models_for(settings)
    common: dict[str, Any] = {
        "models": models,
        "timezone_detected": settings.timezone_detected,
        "strategic": strategic,
    }
    if command == "ask":
        return ask(orchard, clock, text or "", **common)
    return chat(orchard, clock, transcript=transcript, **common)
