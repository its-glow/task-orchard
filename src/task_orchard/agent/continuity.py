"""Conversations for a front end without a terminal's /new habit (SPEC 10.5).

A `Conversation` holds the current session of the Discord bot and decides which
session each message goes to:

1. no session yet: a new one;
2. a reply to one of the assistant's messages in the current session: that session;
3. the session was active less than the idle limit ago: that session;
4. otherwise the continuity check decides.

The check is a judgement and so is made by a model: one request to the basic
tier's model, outside every session, with `continuity.md` as its system prompt.
This module only builds the request and reads the first word of the reply.

It is also the command registry's host for the bot (commands.Host).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from importlib import resources

from task_orchard.agent.loop import SESSION_TOKEN_LIMIT, Session, run_turn, start_session
from task_orchard.agent.model import BASIC, Model, Reply
from task_orchard.agent.trace import Trace
from task_orchard.core.clock import Clock
from task_orchard.core.service import Orchard

CONTINUE = "continue"
NEW = "new"
DEFAULT_IDLE = timedelta(minutes=30)


def continuity_prompt() -> str:
    path = resources.files("task_orchard.agent") / "continuity.md"
    return path.read_text(encoding="utf-8")


def ago(gap: timedelta) -> str:
    """'2 days 3 hours', '1 hour 5 minutes', '40 minutes': the two largest units."""
    minutes = max(int(gap.total_seconds() // 60), 0)
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    parts = [(days, "day"), (hours, "hour"), (minutes, "minute")]
    shown = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in parts if n]
    return " ".join(shown[:2]) or "0 minutes"


def check_message(last_answer: str, gap: timedelta, new_text: str) -> str:
    return (
        f'<last_answer given="{ago(gap)} before the new message">\n{last_answer}\n</last_answer>\n'
        f"\n<new_message>\n{new_text}\n</new_message>"
    )


def decision(reply: Reply) -> str:
    """CONTINUE when the reply's first word is `continue`; anything else is NEW."""
    words = " ".join(b.get("text", "") for b in reply.content if b.get("type") == "text").split()
    first = words[0].strip("`*.!\"'").lower() if words else ""
    return CONTINUE if first == CONTINUE else NEW


@dataclass(frozen=True)
class Turn:
    answer: str
    # The continuity check started a new session for this message.
    new_conversation: bool = False
    # The session has passed SESSION_TOKEN_LIMIT: say once that /new starts afresh.
    long: bool = False


class Conversation:
    terminal = False

    def __init__(
        self,
        orchard: Orchard,
        clock: Clock,
        models: Mapping[str, Model],
        trace: Trace,
        *,
        idle: timedelta = DEFAULT_IDLE,
        timezone_detected: bool = True,
    ) -> None:
        self.orchard = orchard
        self.clock = clock
        self.models = models
        self.trace = trace
        self.idle = idle
        self.timezone_detected = timezone_detected
        self._session: Session | None = None
        self.last_active: datetime | None = None
        self.last_answer = ""
        self.warned = False

    # --- commands.Host ---

    @property
    def session(self) -> Session:
        if self._session is None:
            self.new_session(BASIC)
        assert self._session is not None
        return self._session

    @property
    def started(self) -> bool:
        return self._session is not None

    def new_session(self, tier: str) -> None:
        self.trace.new_session()
        self._session = start_session(
            self.orchard,
            self.clock,
            self.models,
            tier=tier,
            timezone_detected=self.timezone_detected,
            on_event=self.trace,
        )
        self.last_active = None
        self.last_answer = ""
        self.warned = False

    def leave(self) -> None:
        """Nothing to stop: the commands that call it are terminal-only and not offered here."""

    # --- turns ---

    def turn(self, text: str, *, replying: bool = False) -> Turn:
        """Route `text` to a session and run a turn there.

        ModelUnavailable from the check or the turn propagates; a failed check
        leaves the conversation as it was.
        """
        new_conversation = False
        if self._session is None:
            self.new_session(BASIC)
        elif not replying and self._idle_for() >= self.idle and self.check(text) == NEW:
            self.new_session(BASIC)
            new_conversation = True
        session = self.session
        self.trace.new_turn()
        try:
            answer = run_turn(session, text)
        finally:
            self.last_active = self.clock.now()
        self.last_answer = answer
        long = session.input_tokens > SESSION_TOKEN_LIMIT and not self.warned
        self.warned = self.warned or long
        return Turn(answer, new_conversation=new_conversation, long=long)

    def _idle_for(self) -> timedelta:
        if self.last_active is None:
            # A session that has had no turn yet, after /new or /strategic.
            return timedelta(0)
        return self.clock.now() - self.last_active

    def check(self, text: str) -> str:
        """Ask the basic tier's model whether `text` continues the session. CONTINUE or NEW."""
        model = self.models[BASIC]
        message = check_message(self.last_answer, self._idle_for(), text)
        reply = model.respond(
            [{"type": "text", "text": continuity_prompt()}],
            [],
            [{"role": "user", "content": [{"type": "text", "text": message}]}],
        )
        result = decision(reply)
        event = {
            "type": "continuity",
            "decision": result,
            "reply": " ".join(
                b.get("text", "") for b in reply.content if b.get("type") == "text"
            ).strip(),
            "model": str(getattr(model, "model_id", type(model).__name__)),
        }
        if reply.usage is not None:
            event.update(asdict(reply.usage))
        self.trace(event)
        return result
