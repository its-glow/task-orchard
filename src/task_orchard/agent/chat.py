"""`task-orchard chat` and `task-orchard ask` (SPEC 10.2).

The answer goes to stdout. Everything else (the tier in use, one line per tool
call, notices and errors) goes to stderr, so the user can watch what the model
does and still pipe the answers.
"""

from __future__ import annotations

import atexit
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from task_orchard.agent import commands
from task_orchard.agent.loop import SESSION_TOKEN_LIMIT, Session, run_turn, start_session
from task_orchard.agent.model import (
    BASIC,
    EFFORT,
    STRATEGIC,
    AnthropicModel,
    Model,
    ModelUnavailable,
    has_credentials,
)
from task_orchard.agent.trace import Trace
from task_orchard.config import THINKING_OMIT, Settings
from task_orchard.core.clock import Clock
from task_orchard.core.service import Orchard

CREDENTIALS_HELP = (
    "Task Orchard's assistant needs credentials for the Claude API.\n"
    "Set ANTHROPIC_API_KEY, or log in with the ant CLI (ant auth login), then try again.\n"
    "Model calls are billed to that account."
)
PROXY_KEY_HELP = (
    "TASK_ORCHARD_API_BASE_URL is set, so Task Orchard's assistant talks to that gateway, "
    "but TASK_ORCHARD_API_KEY is not.\n"
    "Set it to your key for the gateway, then try again. No other credentials are ever "
    "sent to a gateway."
)
COMMANDS_HELP = "/help lists the commands; /exit leaves."
HISTORY_LINES = 1000
LONG_SESSION = (
    "This conversation is getting long. /new starts a fresh one; your tasks and profile are kept."
)


def models_for(settings: Settings) -> dict[str, Model]:
    return {
        tier: AnthropicModel(
            model_id,
            EFFORT[tier],
            base_url=settings.api_base_url,
            api_key=settings.api_key,
            thinking=settings.thinking != THINKING_OMIT,
        )
        for tier, model_id in ((BASIC, settings.model_basic), (STRATEGIC, settings.model_strategic))
    }


def credentials_problem(settings: Settings, sdk_has_credentials: Callable[[], bool]) -> str | None:
    """What to tell the user when no request could be sent for want of a credential.

    A gateway takes only its own key, so a missing one is a problem even when
    the SDK would find other credentials. Our own key needs no further check.
    """
    if settings.api_base_url and not settings.api_key:
        return PROXY_KEY_HELP
    if settings.api_key or sdk_has_credentials():
        return None
    return CREDENTIALS_HELP


def _line_reader(
    stdin: TextIO, stdout: TextIO, stderr: TextIO, history: Path | None
) -> Callable[[], str]:
    """Return a function giving the next typed line, or "" at end of input.

    At a real terminal, lines come from input(). Where the readline module
    exists, it gives line editing (arrows, Ctrl-A, Ctrl-E) and history kept
    between sessions; without it, a Unix terminal echoes arrow keys as escape
    codes. Windows has no readline, but its console edits input() lines itself
    and keeps the session's history, so only the saved history is lost there.
    Otherwise lines are read plainly, which is what pipes and tests need.
    """
    if stdin is sys.stdin and stdout is sys.stdout and stdin.isatty() and stdout.isatty():
        try:
            import readline
        except ImportError:
            readline = None

        if readline is not None and history is not None:
            readline.set_history_length(HISTORY_LINES)
            try:
                history.parent.mkdir(parents=True, exist_ok=True)
                if history.exists():
                    readline.read_history_file(history)
                atexit.register(readline.write_history_file, history)
            except OSError:
                pass  # no history is better than no chat

        def read_edited() -> str:
            try:
                return input("> ") + "\n"
            except EOFError:
                print(file=stderr)
                return ""

        return read_edited

    interactive = stdin.isatty()

    def read_plain() -> str:
        if interactive:
            print("> ", end="", file=stderr, flush=True)
        return stdin.readline()

    return read_plain


def chat(
    orchard: Orchard,
    clock: Clock,
    *,
    models: Mapping[str, Model],
    timezone_detected: bool = True,
    strategic: bool = False,
    transcript: Path | None = None,
    history: Path | None = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """A terminal conversation. Each line typed is a turn."""
    log = None
    if transcript is not None:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        log = transcript.open("a", encoding="utf-8")
    trace = Trace(log, tool_lines=stderr)
    host = _ChatHost(orchard, clock, models, trace, timezone_detected)
    try:
        print(f"Task Orchard. {COMMANDS_HELP}", file=stderr, flush=True)
        if transcript is not None:
            print(f"Transcript: {transcript}", file=stderr, flush=True)
        host.new_session(STRATEGIC if strategic else BASIC)
        commands.announce(host.session, stderr)
        read_line = _line_reader(stdin, stdout, stderr, history)
        while not host.leaving:
            line = read_line()
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            if text.startswith("/"):
                name, _, rest = text.partition(" ")
                command = commands.find(name)
                if command is None:
                    print(f"Unknown command. {COMMANDS_HELP}", file=stderr, flush=True)
                else:
                    command.run(host, commands.parse_args(command, rest), stdout, stderr)
                continue
            trace.new_turn()
            session = host.session
            try:
                answer = run_turn(session, text)
            except ModelUnavailable as err:
                print(f"Could not reach the model: {err}", file=stderr, flush=True)
                continue
            print(answer, file=stdout, flush=True)
            if session.input_tokens > SESSION_TOKEN_LIMIT and not host.warned:
                print(LONG_SESSION, file=stderr, flush=True)
                host.warned = True
        return 0
    finally:
        if log is not None:
            log.close()


class _ChatHost:
    """chat's side of the command registry (commands.Host)."""

    terminal = True

    def __init__(
        self,
        orchard: Orchard,
        clock: Clock,
        models: Mapping[str, Model],
        trace: Trace,
        timezone_detected: bool,
    ) -> None:
        self.orchard = orchard
        self.clock = clock
        self.models = models
        self.trace = trace
        self.timezone_detected = timezone_detected
        self.session: Session
        self.leaving = False
        # The long-session notice has been shown in this session.
        self.warned = False

    def new_session(self, tier: str) -> None:
        self.trace.new_session()
        self.session = start_session(
            self.orchard,
            self.clock,
            self.models,
            tier=tier,
            timezone_detected=self.timezone_detected,
            on_event=self.trace,
        )
        self.warned = False

    def leave(self) -> None:
        self.leaving = True


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
    trace = Trace(None, tool_lines=stderr)
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
    keep_transcript: bool = True,
    credentials: Callable[[], bool] | None = None,
) -> int:
    """The entry point cli uses. Checks credentials before anything is read.

    `chat` keeps a transcript unless told not to: in `transcript` if given,
    otherwise in a new file under the settings' transcript directory.
    """
    problem = credentials_problem(settings, credentials or has_credentials)
    if problem:
        print(problem, file=sys.stderr)
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
    if not keep_transcript:
        transcript = None
    elif transcript is None:
        stamp = clock.now().strftime("chat-%Y%m%d-%H%M%SZ.jsonl")
        transcript = settings.transcript_dir / stamp
    return chat(orchard, clock, transcript=transcript, history=settings.chat_history, **common)
