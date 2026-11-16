"""SPEC 10.2: the app's tool-use loop, with a scripted model."""

from __future__ import annotations

import json

import pytest

from task_orchard.agent.loop import (
    CUT_OFF_NOTE,
    MAX_STEPS,
    REFUSAL_TEXT,
    STEP_LIMIT_TEXT,
    run_turn,
    start_session,
)
from task_orchard.agent.model import BASIC, STRATEGIC, ModelUnavailable, Reply
from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from tests.scripted import ScriptedModel, calls, results, text


def session_with(orchard: Orchard, clock: FixedClock, *replies, events=None):
    model = ScriptedModel(list(replies))
    session = start_session(
        orchard,
        clock,
        {BASIC: model, STRATEGIC: ScriptedModel([])},
        on_event=None if events is None else events.append,
    )
    return session, model


def payload(block: dict) -> dict:
    return json.loads(block["content"])


def test_a_reply_with_no_tool_call_ends_the_turn(orchard: Orchard, clock: FixedClock) -> None:
    session, model = session_with(orchard, clock, text("Nothing is urgent."))
    assert run_turn(session, "Anything urgent?") == "Nothing is urgent."
    assert len(model.requests) == 1
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


def test_a_tool_call_changes_the_database_and_its_result_goes_back(
    orchard: Orchard, clock: FixedClock
) -> None:
    session, model = session_with(
        orchard,
        clock,
        calls(("capture_tasks", {"items": [{"title": "Buy stamps"}]})),
        text("Noted."),
    )
    assert run_turn(session, "Remind me to buy stamps") == "Noted."
    assert orchard.search_tasks("stamps")["total"] == 1
    sent = model.requests[1].messages[-1]
    [result] = results(sent)
    assert "is_error" not in result
    assert payload(result)["captured"][0]["title"] == "Buy stamps"


def test_two_calls_run_in_order_and_go_back_in_one_message(
    orchard: Orchard, clock: FixedClock
) -> None:
    events: list[dict] = []
    session, model = session_with(
        orchard,
        clock,
        calls(
            ("create_task", {"title": "Plan the trip"}),
            ("search_tasks", {"query": "trip"}),
        ),
        text("Done."),
        events=events,
    )
    run_turn(session, "Add a trip task and show it")
    tool_events = [e for e in events if e["type"] == "tool"]
    assert [e["name"] for e in tool_events] == ["create_task", "search_tasks"]
    found = results(model.requests[1].messages[-1])
    assert len(found) == 2
    # The search ran after the create, so it finds the new task.
    assert payload(found[1])["total"] == 1
    ids = [b["id"] for b in model.requests[1].messages[-2]["content"]]
    assert [r["tool_use_id"] for r in found] == ids


def test_an_error_goes_back_and_a_retry_succeeds(orchard: Orchard, clock: FixedClock) -> None:
    created = orchard.create_task("Fix the gate")["task"]["id"]
    session, model = session_with(
        orchard,
        clock,
        calls(("get_task", {"task_id": 999}), ("get_task", {"task_id": created})),
        calls(("update_task", {"task_id": created, "priority": "urgent"})),
        calls(("update_task", {"task_id": created, "priority": "high"})),
        text("Done."),
    )
    assert run_turn(session, "Make the gate high priority") == "Done."
    first = results(model.requests[1].messages[-1])
    assert first[0]["is_error"] is True and payload(first[0])["error"] == "not_found"
    assert "is_error" not in first[1]  # a failure does not stop the next call
    [bad] = results(model.requests[2].messages[-1])
    assert bad["is_error"] is True and payload(bad)["error"] == "invalid"
    assert orchard.get_task(created)["task"]["fields"]["priority"]["value"] == "high"


def test_the_step_limit(orchard: Orchard, clock: FixedClock) -> None:
    replies = [calls(("read_profile", {})) for _ in range(MAX_STEPS)]
    session, model = session_with(orchard, clock, *replies)
    assert run_turn(session, "Loop forever") == STEP_LIMIT_TEXT
    assert len(model.requests) == MAX_STEPS
    # Every call made got its result; the next turn's message follows them.
    assert results(session.messages[-1])


def test_max_tokens_and_refusal(orchard: Orchard, clock: FixedClock) -> None:
    session, _ = session_with(
        orchard, clock, text("The first half", stop="max_tokens"), text("", stop="refusal")
    )
    assert run_turn(session, "Write a lot") == f"The first half\n\n{CUT_OFF_NOTE}"
    assert run_turn(session, "Something else") == REFUSAL_TEXT


def test_a_cut_off_tool_call_is_answered_as_not_run(orchard: Orchard, clock: FixedClock) -> None:
    cut = Reply(
        content=[
            {"type": "text", "text": "Let me add those."},
            {"type": "tool_use", "id": "toolu_cut", "name": "capture_tasks", "input": {}},
        ],
        stop="max_tokens",
    )
    session, model = session_with(orchard, clock, cut, text("Added."))
    answer = run_turn(session, "Add stamps, milk and bread")
    assert answer.endswith(CUT_OFF_NOTE)
    [not_run] = results(session.messages[-1])
    assert not_run["tool_use_id"] == "toolu_cut" and not_run["is_error"] is True
    assert payload(not_run)["error"] == "not_run"
    assert orchard.search_tasks()["total"] == 0  # nothing ran

    run_turn(session, "continue")
    sent = model.requests[1].messages
    # Every tool call in the next request is followed by its result.
    for i, message in enumerate(sent):
        if message["role"] != "assistant":
            continue
        asked = {b["id"] for b in message["content"] if b.get("type") == "tool_use"}
        if asked:
            answered = {r["tool_use_id"] for r in results(sent[i + 1])}
            assert asked <= answered


def test_requests_only_ever_grow(orchard: Orchard, clock: FixedClock) -> None:
    session, model = session_with(
        orchard,
        clock,
        calls(("read_profile", {})),
        text("Hello."),
        calls(("write_profile_note", {"topic": "energy", "text": "Low after 3pm."})),
        text("Saved."),
        text("Bye."),
    )
    run_turn(session, "Hi")
    run_turn(session, "Remember I'm low after 3pm")
    clock.advance(hours=2)
    run_turn(session, "Bye")
    requests = model.requests
    assert len(requests) == 5
    for before, after in zip(requests, requests[1:], strict=False):
        assert after.system == before.system
        assert after.tools == before.tools
        assert after.messages[: len(before.messages)] == before.messages
        assert len(after.messages) > len(before.messages)


def test_a_reply_is_passed_back_unchanged(orchard: Orchard, clock: FixedClock) -> None:
    odd = Reply(
        content=[
            {"type": "thinking", "thinking": "", "signature": "abc=="},
            {"type": "some_future_block", "data": {"x": [1, 2]}},
            {"type": "text", "text": "Fine."},
        ],
        stop="end",
    )
    session, model = session_with(orchard, clock, odd, text("Again."))
    assert run_turn(session, "Hi") == "Fine."
    run_turn(session, "Again")
    assert model.requests[1].messages[1] == {"role": "assistant", "content": odd.content}


def test_a_model_that_raises_keeps_what_was_appended(orchard: Orchard, clock: FixedClock) -> None:
    session, model = session_with(
        orchard,
        clock,
        calls(("capture_tasks", {"items": [{"title": "Buy stamps"}]})),
        ModelUnavailable("connection refused"),
        text("Noted, and it's in your inbox."),
    )
    with pytest.raises(ModelUnavailable):
        run_turn(session, "Remind me to buy stamps")
    # The capture happened and its result stays in the conversation.
    assert orchard.search_tasks("stamps")["total"] == 1
    assert [m["role"] for m in session.messages] == ["user", "assistant", "user"]
    assert results(session.messages[-1])

    assert run_turn(session, "Did that work?") == "Noted, and it's in your inbox."
    assert model.requests[-1].messages[:3] == session.messages[:3]


def test_switching_tier_keeps_the_conversation(orchard: Orchard, clock: FixedClock) -> None:
    basic = ScriptedModel([text("Basic answer.")])
    strategic = ScriptedModel([text("Strategic answer.")])
    session = start_session(orchard, clock, {BASIC: basic, STRATEGIC: strategic})
    run_turn(session, "Small question")
    session.switch(STRATEGIC)
    assert run_turn(session, "Big question") == "Strategic answer."
    first, second = basic.requests[0], strategic.requests[0]
    assert second.system == first.system and second.tools == first.tools
    assert second.messages[: len(first.messages)] == first.messages
