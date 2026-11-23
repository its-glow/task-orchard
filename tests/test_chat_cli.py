"""SPEC 10.2: task-orchard chat and ask, with a scripted model injected."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from task_orchard import cli
from task_orchard.agent import chat as chat_module
from task_orchard.agent.chat import CREDENTIALS_HELP, LONG_SESSION, ask, chat
from task_orchard.agent.loop import SESSION_TOKEN_LIMIT
from task_orchard.agent.model import BASIC, STRATEGIC, ModelUnavailable, Reply, Usage, cost
from task_orchard.agent.trace import tool_line
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from tests.scripted import ScriptedModel, calls, text


def run_chat(orchard, clock, lines, *, basic=(), deep=(), **kw):
    """Run chat on scripted lines. `deep` holds the strategic model's replies."""
    models = {
        BASIC: ScriptedModel(list(basic), "small-1"),
        STRATEGIC: ScriptedModel(list(deep), "big-1"),
    }
    out, err = io.StringIO(), io.StringIO()
    code = chat(
        orchard,
        clock,
        models=models,
        stdin=io.StringIO("".join(f"{line}\n" for line in lines)),
        stdout=out,
        stderr=err,
        **kw,
    )
    return code, out.getvalue(), err.getvalue(), models


def test_ask_prints_the_answer(orchard: Orchard, clock: FixedClock) -> None:
    out, err = io.StringIO(), io.StringIO()
    models = {BASIC: ScriptedModel([text("Do the release notes.")]), STRATEGIC: ScriptedModel([])}
    code = ask(orchard, clock, "What now?", models=models, stdout=out, stderr=err)
    assert code == 0
    assert out.getvalue() == "Do the release notes.\n"


def test_ask_exits_1_when_the_model_cannot_be_reached(orchard: Orchard, clock: FixedClock) -> None:
    out, err = io.StringIO(), io.StringIO()
    models = {
        BASIC: ScriptedModel([ModelUnavailable("no route to host")]),
        STRATEGIC: ScriptedModel([]),
    }
    code = ask(orchard, clock, "What now?", models=models, stdout=out, stderr=err)
    assert code == 1
    assert out.getvalue() == ""
    assert "no route to host" in err.getvalue()


def test_ask_strategic_uses_the_strategic_model(orchard: Orchard, clock: FixedClock) -> None:
    out = io.StringIO()
    models = {BASIC: ScriptedModel([]), STRATEGIC: ScriptedModel([text("Big think.")])}
    ask(
        orchard,
        clock,
        "Plan my week",
        models=models,
        strategic=True,
        stdout=out,
        stderr=io.StringIO(),
    )
    assert out.getvalue() == "Big think.\n"


def test_chat_answers_on_stdout_and_reports_tools_on_stderr(
    orchard: Orchard, clock: FixedClock
) -> None:
    code, out, err, _ = run_chat(
        orchard,
        clock,
        ["What could I do with 30 minutes?"],
        basic=[
            calls(("get_candidates", {"minutes_available": 30, "energy": "low"})),
            calls(("get_task", {"task_id": 999})),
            text("Buy milk."),
        ],
    )
    assert code == 0
    assert out == "Buy milk.\n"
    assert '-> get_candidates(minutes_available=30, energy="low")\n' in err
    assert "-> get_task(task_id=999) !! not_found\n" in err
    assert "get_candidates" not in out


def test_new_starts_a_session_that_reads_the_profile_again(
    orchard: Orchard, clock: FixedClock
) -> None:
    code, out, err, models = run_chat(
        orchard,
        clock,
        ["Remember I'm low after 3pm", "/new", "Hello again"],
        basic=[
            calls(("write_profile_note", {"topic": "energy", "text": "Low after 3pm."})),
            text("Saved."),
            text("Hello."),
        ],
    )
    assert out == "Saved.\nHello.\n"
    first, _, third = models[BASIC].requests
    assert "Low after 3pm." not in first.system[1]["text"]
    assert "Low after 3pm." in third.system[1]["text"]
    assert len(third.messages) == 1  # a fresh conversation


def test_switching_tier_in_chat(orchard: Orchard, clock: FixedClock) -> None:
    code, out, err, models = run_chat(
        orchard,
        clock,
        ["Small thing", "/strategic", "Big thing", "/basic", "Small again"],
        basic=[text("one"), text("three")],
        deep=[text("two")],
    )
    assert out == "one\ntwo\nthree\n"
    assert "[basic: small-1]" in err and "[strategic: big-1]" in err
    # The strategic model saw the whole conversation so far.
    assert len(models[STRATEGIC].requests[0].messages) == 3


def test_chat_strategic_flag_and_new_returns_to_basic(orchard: Orchard, clock: FixedClock) -> None:
    code, out, err, models = run_chat(
        orchard,
        clock,
        ["Plan", "/new", "Chat"],
        basic=[text("chat")],
        deep=[text("plan")],
        strategic=True,
    )
    assert out == "plan\nchat\n"


def test_quit_and_end_of_input_exit(orchard: Orchard, clock: FixedClock) -> None:
    code, out, _, models = run_chat(orchard, clock, ["/quit", "never read"], basic=[])
    assert code == 0 and out == "" and models[BASIC].requests == []
    code, out, _, _ = run_chat(orchard, clock, [], basic=[])
    assert code == 0 and out == ""
    code, out, _, models = run_chat(orchard, clock, ["/exit", "never read"], basic=[])
    assert code == 0 and out == "" and models[BASIC].requests == []


def test_a_model_failure_ends_the_turn_not_the_chat(orchard: Orchard, clock: FixedClock) -> None:
    code, out, err, _ = run_chat(
        orchard,
        clock,
        ["Hi", "Hi again"],
        basic=[ModelUnavailable("rate limited"), text("Hello.")],
    )
    assert code == 0
    assert out == "Hello.\n"
    assert "rate limited" in err


def test_the_transcript_has_one_json_line_per_message_and_tool_outcome(
    orchard: Orchard, clock: FixedClock, tmp_path: Path
) -> None:
    path = tmp_path / "chat.jsonl"
    run_chat(
        orchard,
        clock,
        ["Show my profile"],
        basic=[calls(("read_profile", {})), text("Here it is.")],
        transcript=path,
    )
    records = [json.loads(line) for line in path.read_text().splitlines()]
    kinds = [(r["type"], r.get("message", {}).get("role"), r.get("name")) for r in records]
    assert kinds == [
        ("message", "user", None),
        ("message", "assistant", None),
        ("tool", None, "read_profile"),
        ("message", "user", None),
        ("message", "assistant", None),
    ]
    assert all(r["session"] == 1 for r in records)


def test_the_transcript_path_is_announced(
    orchard: Orchard, clock: FixedClock, tmp_path: Path
) -> None:
    path = tmp_path / "chat.jsonl"
    _, _, err, _ = run_chat(orchard, clock, [], transcript=path)
    assert f"Transcript: {path}" in err


def _main_transcript(monkeypatch, orchard, clock, tmp_path, **kw):
    """Run main for chat with the conversation itself stubbed out; return the transcript."""
    seen = {}
    monkeypatch.setattr(chat_module, "models_for", lambda settings: {})
    monkeypatch.setattr(chat_module, "chat", lambda *a, **k: seen.update(k) or 0)
    settings = cli.load_settings({"XDG_CACHE_HOME": str(tmp_path / "cache")})
    code = chat_module.main(
        "chat", settings, lambda: (orchard, clock), credentials=lambda: True, **kw
    )
    assert code == 0
    return seen["transcript"]


def test_chat_keeps_a_transcript_by_default(
    monkeypatch, orchard: Orchard, clock: FixedClock, tmp_path: Path
) -> None:
    path = _main_transcript(monkeypatch, orchard, clock, tmp_path)
    assert path.parent == tmp_path / "cache" / "task-orchard" / "transcripts"
    assert path.name == clock.now().strftime("chat-%Y%m%d-%H%M%SZ.jsonl")
    explicit = tmp_path / "mine.jsonl"
    assert _main_transcript(monkeypatch, orchard, clock, tmp_path, transcript=explicit) == explicit
    assert _main_transcript(monkeypatch, orchard, clock, tmp_path, keep_transcript=False) is None


def test_chat_creates_the_transcript_directory(
    orchard: Orchard, clock: FixedClock, tmp_path: Path
) -> None:
    path = tmp_path / "new" / "dir" / "chat.jsonl"
    run_chat(orchard, clock, [], transcript=path)
    assert path.exists()


def test_the_long_session_notice_appears_once(orchard: Orchard, clock: FixedClock) -> None:
    big = SESSION_TOKEN_LIMIT + 1
    code, out, err, _ = run_chat(
        orchard,
        clock,
        ["one", "two", "three"],
        basic=[text("a", tokens=10), text("b", tokens=big), text("c", tokens=big + 5)],
    )
    assert err.count(LONG_SESSION) == 1
    assert err.index(LONG_SESSION) > 0


def test_tool_line_format() -> None:
    line = tool_line(
        {"type": "tool", "name": "get_task", "input": {"task_id": 4}, "is_error": False}
    )
    assert line == "-> get_task(task_id=4)"


class Untouchable(io.StringIO):
    def readline(self, *args, **kwargs):  # type: ignore[override]
        raise AssertionError("input was read")

    def read(self, *args, **kwargs):  # type: ignore[override]
        raise AssertionError("input was read")


@pytest.mark.parametrize("argv", [["chat"], ["ask", "What now?"]])
def test_missing_credentials_exit_1_before_any_input(
    argv, monkeypatch, capsys, tmp_path: Path
) -> None:
    monkeypatch.setenv("TASK_ORCHARD_DB", str(tmp_path / "never.db"))
    monkeypatch.setattr(chat_module, "has_credentials", lambda: False)
    monkeypatch.setattr("sys.stdin", Untouchable())
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert CREDENTIALS_HELP in captured.err
    assert captured.out == ""
    assert not (tmp_path / "never.db").exists()  # nothing was opened either


# --- /help and the tester commands (SPEC 10.2). None of them reaches the model. ---


def test_help_lists_every_command(orchard: Orchard, clock: FixedClock) -> None:
    _, out, err, models = run_chat(orchard, clock, ["/help", "/nonsense"])
    for command in ("/new", "/strategic", "/basic", "/help", "/exit", "/quit", "/tools",
                    "/tool", "/last", "/prompt", "/usage"):  # fmt: skip
        assert command in err, command
    assert "Unknown command" in err and "/help" in err.split("Unknown command")[1]
    assert out == "" and models[BASIC].requests == []


def test_tools_lists_the_catalogue(orchard: Orchard, clock: FixedClock) -> None:
    _, out, _, models = run_chat(orchard, clock, ["/tools"])
    assert "get_candidates(" in out and "minutes_available?" in out
    assert "task_id)" in out  # get_task's one required parameter
    assert "read-only" in out
    assert models[BASIC].requests == []


def test_tool_runs_a_read_only_tool_without_the_model(
    orchard: Orchard, clock: FixedClock, seeded
) -> None:
    line = f'/tool get_task {{"task_id": {seeded["milk"]}}}'
    _, out, err, models = run_chat(orchard, clock, [line, "/tool read_profile"])
    first = out.index("\n}\n") + 3
    assert json.loads(out[:first])["task"]["title"] == "Buy milk"
    assert "now" in json.loads(out[first:])
    assert models[BASIC].requests == []


def test_tool_refuses_writes_and_bad_input(orchard: Orchard, clock: FixedClock, seeded) -> None:
    before = orchard.get_task(seeded["milk"])["task"]
    _, out, err, _ = run_chat(
        orchard,
        clock,
        [
            f'/tool drop_tasks {{"task_ids": [{seeded["milk"]}]}}',
            "/tool get_task {not json",
            "/tool no_such_tool",
            "/tool",
        ],
    )
    assert orchard.get_task(seeded["milk"])["task"] == before
    assert "read-only" in err and "not valid JSON" in err and "no_such_tool" in err
    assert "Usage: /tool NAME" in err


def test_tool_errors_print_the_payload(orchard: Orchard, clock: FixedClock) -> None:
    _, out, _, _ = run_chat(orchard, clock, ['/tool get_task {"task_id": 99999}'])
    assert json.loads(out)["error"] == "not_found"


def test_last_shows_the_full_results_of_the_last_turn(orchard: Orchard, clock: FixedClock) -> None:
    _, out, _, _ = run_chat(
        orchard,
        clock,
        ["/last", "Profile?", "/last"],
        basic=[calls(("read_profile", {})), text("Here.")],
    )
    answer, shown = out.split("Here.\n")
    assert answer == ""
    assert shown.startswith("-> read_profile()\n")
    assert json.loads(shown.split("\n", 1)[1])["now"]


def test_last_before_any_tool_call(orchard: Orchard, clock: FixedClock) -> None:
    _, out, err, _ = run_chat(orchard, clock, ["/last"])
    assert out == "" and "Nothing to show" in err


def test_last_shows_reasoning_and_tool_calls_in_order(orchard: Orchard, clock: FixedClock) -> None:
    def thinking(text: str) -> dict:
        return {"type": "thinking", "thinking": text, "signature": "sig"}

    asked = calls(("read_profile", {}))
    first = Reply(content=[thinking("Check the profile first."), *asked.content], stop="tool_use")
    then = Reply(content=[thinking(""), thinking("It is empty.\nSay so."), *text("Empty.").content],
                 stop="end")  # fmt: skip
    _, out, _, _ = run_chat(orchard, clock, ["Profile?", "/last"], basic=[first, then])
    shown = out.split("Empty.\n")[1]
    assert shown.startswith("[thinking]\n  Check the profile first.\n-> read_profile()\n{")
    assert shown.endswith("[thinking]\n  It is empty.\n  Say so.\n")  # empty summaries skipped


def test_prompt_prints_the_system_prompt(orchard: Orchard, clock: FixedClock) -> None:
    _, out, _, _ = run_chat(orchard, clock, ["/prompt"])
    assert "You are Task Orchard's assistant." in out
    assert "## Guide: triage" in out
    assert "cached" in out


def test_usage_reports_each_request(orchard: Orchard, clock: FixedClock) -> None:
    figures = Usage(input=300, cache_write=12_000, cache_read=0, output=200)
    reply = Reply(content=[{"type": "text", "text": "Hi."}], stop="end", usage=figures)
    models = {BASIC: ScriptedModel([reply], "claude-haiku-5-5"), STRATEGIC: ScriptedModel([])}
    out, err = io.StringIO(), io.StringIO()
    chat(
        orchard, clock, models=models, stdin=io.StringIO("Hello\n/usage\n"), stdout=out, stderr=err
    )
    shown = out.getvalue().split("Hi.\n")[1]
    assert "claude-haiku-5-5" in shown and "12,000" in shown and "$" in shown


def test_usage_is_written_to_the_transcript(
    orchard: Orchard, clock: FixedClock, tmp_path: Path
) -> None:
    figures = Usage(input=10, cache_write=0, cache_read=5000, output=50)
    reply = Reply(content=[{"type": "text", "text": "Hi."}], stop="end", usage=figures)
    models = {BASIC: ScriptedModel([reply], "m"), STRATEGIC: ScriptedModel([])}
    path = tmp_path / "t.jsonl"
    chat(orchard, clock, models=models, stdin=io.StringIO("Hello\n"), stdout=io.StringIO(),
         stderr=io.StringIO(), transcript=path)  # fmt: skip
    records = [json.loads(line) for line in path.read_text().splitlines()]
    usage = [r for r in records if r["type"] == "usage"]
    assert usage == [
        {"session": 1, "type": "usage", "model": "m", "input": 10, "cache_write": 0,
         "cache_read": 5000, "output": 50},
    ]  # fmt: skip


def test_cost_estimate() -> None:
    short = Usage(input=50_000, cache_write=10_000, cache_read=20_000, output=1_000)
    expected = (50_000 * 0.10 + 10_000 * 0.125 + 20_000 * 0.01 + 1_000 * 0.50) / 1e6
    assert cost("claude-haiku-5-5", short) == pytest.approx(expected)
    long = Usage(input=200_000)  # over 100k: Haiku 5.5's long-prompt rate, x5
    assert cost("claude-haiku-5-5", long) == pytest.approx(0.10)
    assert cost("unknown-model", short) is None


def test_the_transcript_keeps_the_reasoning_summary(
    orchard: Orchard, clock: FixedClock, tmp_path: Path
) -> None:
    thought = {"type": "thinking", "thinking": "The user wants a greeting.", "signature": "sig"}
    reply = Reply(content=[thought, {"type": "text", "text": "Hi."}], stop="end")
    models = {BASIC: ScriptedModel([reply], "m"), STRATEGIC: ScriptedModel([])}
    path = tmp_path / "t.jsonl"
    chat(orchard, clock, models=models, stdin=io.StringIO("Hello\n"), stdout=io.StringIO(),
         stderr=io.StringIO(), transcript=path)  # fmt: skip
    records = [json.loads(line) for line in path.read_text().splitlines()]
    answer = [r for r in records if r.get("message", {}).get("role") == "assistant"][0]
    assert answer["message"]["content"][0] == thought
