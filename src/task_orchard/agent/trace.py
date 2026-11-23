"""What a front end keeps of a conversation besides the conversation itself.

`Trace` is passed to every session as its listener (SPEC 10.2). It writes the
transcript, one JSON line per event tagged with the session's number, and keeps
what the tester commands show: the latest turn's reasoning summaries and tool
calls, in order (/last), and the usage of every request in the current session
(/usage). `chat` also has it
report each tool call on stderr; the Discord bot does not (SPEC 10.5).
"""

from __future__ import annotations

import json
from typing import TextIO

from task_orchard.agent.loop import Event


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


class Trace:
    def __init__(self, transcript: TextIO | None, tool_lines: TextIO | None = None) -> None:
        self.transcript = transcript
        self.tool_lines = tool_lines
        self.session_number = 0
        # The latest turn's `thinking` and `tool` events, in the order they came.
        self.turn: list[Event] = []
        self.requests: list[Event] = []

    def new_session(self) -> None:
        self.session_number += 1
        self.turn = []
        self.requests = []

    def new_turn(self) -> None:
        self.turn = []

    def __call__(self, event: Event) -> None:
        if event["type"] == "message" and event["message"].get("role") == "assistant":
            for block in event["message"].get("content") or []:
                text = block.get("thinking") if block.get("type") == "thinking" else None
                if text and text.strip():
                    self.turn.append({"type": "thinking", "text": text.strip()})
        elif event["type"] == "tool":
            if self.tool_lines is not None:
                print(tool_line(event), file=self.tool_lines, flush=True)
            self.turn.append(event)
        elif event["type"] == "usage":
            self.requests.append(event)
        if self.transcript is not None:
            record = {"session": self.session_number, **event}
            self.transcript.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.transcript.flush()
