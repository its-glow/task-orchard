"""AnthropicModel's request and reply handling, checked offline with a fake client.

Nothing here reaches the network: the client is replaced before first use.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from task_orchard.agent.chat import models_for
from task_orchard.agent.model import (
    BASIC,
    EFFORT,
    STRATEGIC,
    AnthropicModel,
    ModelUnavailable,
)
from task_orchard.config import load_settings

SYSTEM = [{"type": "text", "text": "stable"}, {"type": "text", "text": "profile"}]
TOOLS = [{"name": "read_profile", "description": "d", "input_schema": {"type": "object"}}]
MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


class FakeBlock:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def to_dict(self, mode: str = "python") -> dict[str, Any]:
        return dict(self.data)


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.sent: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def response(stop: str, *blocks: dict[str, Any]) -> Any:
    usage = SimpleNamespace(
        input_tokens=100, cache_creation_input_tokens=None, cache_read_input_tokens=4000
    )
    return SimpleNamespace(content=[FakeBlock(b) for b in blocks], stop_reason=stop, usage=usage)


def with_fake(model: AnthropicModel, messages: FakeMessages) -> AnthropicModel:
    model._client = SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))
    return model


def test_the_tiers_come_from_the_settings(tmp_path) -> None:
    settings = load_settings({"TASK_ORCHARD_DB": str(tmp_path / "x.db"), "TASK_ORCHARD_TZ": "UTC"})
    assert settings.model_basic == settings.model_strategic == "claude-haiku-5-5"
    models = models_for(
        load_settings(
            {
                "TASK_ORCHARD_DB": str(tmp_path / "x.db"),
                "TASK_ORCHARD_TZ": "UTC",
                "TASK_ORCHARD_MODEL_STRATEGIC": "claude-opus-5-5",
            }
        )
    )
    assert (models[BASIC].model_id, models[BASIC].effort) == ("claude-haiku-5-5", "medium")
    assert (models[STRATEGIC].model_id, models[STRATEGIC].effort) == ("claude-opus-5-5", "high")
    assert EFFORT == {BASIC: "medium", STRATEGIC: "high"}


def test_the_request_caches_the_stable_block_and_the_conversation() -> None:
    kwargs = AnthropicModel("claude-haiku-5-5", "medium").request(SYSTEM, TOOLS, MESSAGES)
    assert kwargs["model"] == "claude-haiku-5-5"
    assert kwargs["output_config"] == {"effort": "medium"}
    assert kwargs["cache_control"] == {"type": "ephemeral"}
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in kwargs["system"][1]
    assert "cache_control" not in SYSTEM[0]  # the session's prompt is not touched
    assert "thinking" not in kwargs
    assert "fallbacks" not in kwargs and "betas" not in kwargs  # Haiku has none


def test_fallback_is_requested_only_where_supported() -> None:
    kwargs = AnthropicModel("claude-opus-5-5", "high").request(SYSTEM, TOOLS, MESSAGES)
    assert kwargs["fallbacks"] == "default"
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]


def test_a_reply_is_read_whole() -> None:
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "toolu_1", "name": "read_profile", "input": {}},
    ]
    fake = FakeMessages(response("tool_use", *blocks))
    reply = with_fake(AnthropicModel("claude-haiku-5-5", "medium"), fake).respond(
        SYSTEM, TOOLS, MESSAGES
    )
    assert reply.content == blocks
    assert reply.stop == "tool_use"
    assert reply.input_tokens == 4100


@pytest.mark.parametrize(
    ("api", "ours"),
    [
        ("end_turn", "end"),
        ("max_tokens", "max_tokens"),
        ("refusal", "refusal"),
        ("stop_sequence", "end"),
        ("pause_turn", "end"),
    ],
)
def test_stop_reasons(api: str, ours: str) -> None:
    fake = FakeMessages(response(api, {"type": "text", "text": "x"}))
    reply = with_fake(AnthropicModel("claude-haiku-5-5", "medium"), fake).respond(
        SYSTEM, TOOLS, MESSAGES
    )
    assert reply.stop == ours


def test_an_api_failure_is_model_unavailable() -> None:
    error = anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]
    fake = FakeMessages(error=error)
    model = with_fake(AnthropicModel("claude-haiku-5-5", "medium"), fake)
    with pytest.raises(ModelUnavailable):
        model.respond(SYSTEM, TOOLS, MESSAGES)
