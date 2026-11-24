"""Reaching the model directly or through a gateway such as LiteLLM (SPEC 10.2, the model).

Nothing here reaches the network: clients are built but never sent a request.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from task_orchard import cli
from task_orchard.agent import chat as chat_module
from task_orchard.agent.chat import CREDENTIALS_HELP, PROXY_KEY_HELP, models_for
from task_orchard.agent.commands import announce
from task_orchard.agent.model import BASIC, STRATEGIC, AnthropicModel, Usage, base_model, cost
from task_orchard.config import load_settings
from task_orchard.discord_bot import client as discord_client

SYSTEM = [{"type": "text", "text": "stable"}, {"type": "text", "text": "profile"}]
TOOLS = [{"name": "read_profile", "description": "d", "input_schema": {"type": "object"}}]
MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
PROXY = "https://llm.example.com"


def settings(**env: str):
    return load_settings({"TASK_ORCHARD_DB": "/nonexistent/x.db", "TASK_ORCHARD_TZ": "UTC", **env})


@pytest.fixture
def own_credentials(monkeypatch) -> None:
    """Credentials of the user's own, as the SDK would find them in the environment."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "own-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "own-token")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


# --- settings ---


def test_by_default_the_model_is_reached_directly() -> None:
    s = settings()
    assert s.api_base_url is None and s.api_key is None
    assert s.thinking == "summarized"
    model = models_for(s)[BASIC]
    assert model.base_url is None and model.via is None


def test_the_gateway_settings_reach_both_tiers_and_the_key_is_never_shown() -> None:
    s = settings(
        TASK_ORCHARD_API_BASE_URL=PROXY,
        TASK_ORCHARD_API_KEY="work-key",
        TASK_ORCHARD_MODEL_BASIC="anthropic.claude-haiku-5-5",
    )
    assert "work-key" not in repr(s)
    for tier in (BASIC, STRATEGIC):
        model = models_for(s)[tier]
        assert (model.base_url, model.api_key) == (PROXY, "work-key")
        assert model.via == "llm.example.com"
    assert models_for(s)[BASIC].model_id == "anthropic.claude-haiku-5-5"


def test_an_unknown_thinking_setting_fails_loudly() -> None:
    assert settings(TASK_ORCHARD_THINKING="omit").thinking == "omit"
    with pytest.raises(ValueError, match="TASK_ORCHARD_THINKING"):
        settings(TASK_ORCHARD_THINKING="off")


# --- the client ---


def test_through_a_gateway_only_its_key_is_used(own_credentials) -> None:
    model = AnthropicModel("anthropic.claude-haiku-5-5", "medium", base_url=PROXY, api_key="w")
    model._messages_api()
    client = model._client
    assert str(client.base_url).rstrip("/") == PROXY
    assert client.api_key == "w"
    assert client.auth_token is None  # the user's own token is not sent along


def test_directly_the_sdk_finds_its_own_credentials(own_credentials) -> None:
    model = AnthropicModel("claude-haiku-5-5", "medium")
    model._messages_api()
    client = model._client
    assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"
    assert client.api_key == "own-key"


# --- the request ---


def test_through_a_gateway_the_name_is_sent_as_given_and_fallback_is_never_asked() -> None:
    model = AnthropicModel("anthropic.claude-opus-5-5", "high", base_url=PROXY, api_key="w")
    kwargs = model.request(SYSTEM, TOOLS, MESSAGES)
    assert kwargs["model"] == "anthropic.claude-opus-5-5"
    assert "fallbacks" not in kwargs and "betas" not in kwargs
    # Even a bare name that supports fallback directly.
    kwargs = AnthropicModel("claude-opus-5-5", "high", base_url=PROXY, api_key="w").request(
        SYSTEM, TOOLS, MESSAGES
    )
    assert "fallbacks" not in kwargs
    assert kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_thinking_can_be_left_to_the_endpoint() -> None:
    model = AnthropicModel("claude-haiku-5-5", "medium", thinking=False)
    assert "thinking" not in model.request(SYSTEM, TOOLS, MESSAGES)
    assert models_for(settings(TASK_ORCHARD_THINKING="omit"))[BASIC].thinking is False


# --- names and prices ---


@pytest.mark.parametrize(
    "name",
    [
        "claude-haiku-5-5",
        "anthropic.claude-haiku-5-5",
        "anthropic/claude-haiku-5-5",
        "us.anthropic.claude-haiku-5-5-v1:0",
        "bedrock/anthropic.claude-haiku-5-5",
        "claude-haiku-5-5@20261001",
    ],
)
def test_a_gateway_name_is_priced_as_the_model_it_names(name: str) -> None:
    assert base_model(name) == "claude-haiku-5-5"
    usage = Usage(input=1000, cache_write=0, cache_read=0, output=100)
    assert cost(name, usage) == cost("claude-haiku-5-5", usage)


def test_an_unknown_model_has_no_price() -> None:
    assert cost("anthropic.some-other-model", Usage(1, 0, 0, 1)) is None


# --- credentials: a gateway never falls back to the user's own ---


class Untouchable(io.StringIO):
    def readline(self, *args, **kwargs):  # type: ignore[override]
        raise AssertionError("input was read")


def test_a_gateway_without_its_key_stops_before_anything_even_with_own_credentials(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    monkeypatch.setenv("TASK_ORCHARD_DB", str(tmp_path / "never.db"))
    monkeypatch.setenv("TASK_ORCHARD_API_BASE_URL", PROXY)
    monkeypatch.delenv("TASK_ORCHARD_API_KEY", raising=False)
    monkeypatch.setattr(chat_module, "has_credentials", lambda: True)
    monkeypatch.setattr("sys.stdin", Untouchable())
    assert cli.main(["chat"]) == 1
    err = capsys.readouterr().err
    assert PROXY_KEY_HELP in err and CREDENTIALS_HELP not in err
    assert not (tmp_path / "never.db").exists()


def test_a_key_of_ours_is_enough_without_asking_the_sdk() -> None:
    def sdk_check() -> bool:
        raise AssertionError("the SDK's credentials were consulted")

    s = settings(TASK_ORCHARD_API_BASE_URL=PROXY, TASK_ORCHARD_API_KEY="w")
    assert chat_module.credentials_problem(s, sdk_check) is None
    assert chat_module.credentials_problem(settings(), lambda: True) is None
    assert chat_module.credentials_problem(settings(), lambda: False) == CREDENTIALS_HELP


def test_the_discord_bot_also_refuses_a_gateway_without_its_key(capsys) -> None:
    s = settings(
        TASK_ORCHARD_API_BASE_URL=PROXY,
        TASK_ORCHARD_DISCORD_TOKEN="t",
        TASK_ORCHARD_DISCORD_USER_ID="1",
    )
    assert discord_client.main(s, credentials=lambda: True) == 1
    assert PROXY_KEY_HELP in capsys.readouterr().err


# --- showing where requests go ---


def test_info_shows_the_endpoint_and_never_the_key(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setenv("TASK_ORCHARD_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("TASK_ORCHARD_API_BASE_URL", PROXY)
    monkeypatch.setenv("TASK_ORCHARD_API_KEY", "work-key")
    monkeypatch.setenv("TASK_ORCHARD_THINKING", "omit")
    assert cli.main(["info"]) == 0
    out = capsys.readouterr().out
    assert f"model endpoint: {PROXY}" in out
    assert "TASK_ORCHARD_API_KEY" in out and "work-key" not in out
    assert "thinking: omit" in out


def test_info_shows_a_direct_endpoint(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setenv("TASK_ORCHARD_DB", str(tmp_path / "x.db"))
    monkeypatch.delenv("TASK_ORCHARD_API_BASE_URL", raising=False)
    monkeypatch.delenv("TASK_ORCHARD_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert cli.main(["info"]) == 0
    out = capsys.readouterr().out
    assert "model endpoint: the Claude API, directly" in out
    assert "thinking: summarized" in out


def test_the_tier_line_names_the_gateway() -> None:
    class S:
        tier = BASIC
        model = AnthropicModel("anthropic.claude-haiku-5-5", "medium", base_url=PROXY, api_key="w")

    err = io.StringIO()
    announce(S(), err)  # type: ignore[arg-type]
    assert err.getvalue() == "[basic: anthropic.claude-haiku-5-5 via llm.example.com]\n"
    S.model = AnthropicModel("claude-haiku-5-5", "medium")
    err = io.StringIO()
    announce(S(), err)  # type: ignore[arg-type]
    assert err.getvalue() == "[basic: claude-haiku-5-5]\n"
