"""The one seam through which the app reaches a model (SPEC 10.2, the model).

The loop sees only the Model protocol and Reply. AnthropicModel is the only
code in Task Orchard that imports the vendor SDK; tests use a scripted model
instead, so the test suite never calls a model.

Requests follow the current Claude API documentation:
- thinking is left to the model's default (adaptive on current models), and
  depth is set with output_config.effort, fixed per tier;
- the stable first block of the system prompt carries a cache marker, and the
  request also uses top-level automatic caching, so the conversation so far is
  cached for the next call of a turn;
- reply content is converted with the SDK's own to_dict, so every block,
  including reasoning blocks, goes back exactly as it came;
- server-side fallback on a refusal is requested only for models known to
  support it. Haiku 5.5, the default, does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

BASIC = "basic"
STRATEGIC = "strategic"
TIERS = (BASIC, STRATEGIC)
EFFORT = {BASIC: "medium", STRATEGIC: "high"}

MAX_TOKENS = 16_000
# Models that support server-side fallback on a refusal (Claude API docs, 2026-11).
FALLBACK_MODELS = frozenset(
    {
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
        "claude-fable-5-2",
        "claude-fable-5-1",
    }
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"

Stop = Literal["end", "tool_use", "max_tokens", "refusal"]
_STOPS: dict[str, Stop] = {
    "end_turn": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


@dataclass(frozen=True)
class Reply:
    # The reply's blocks as plain dicts, opaque to the loop apart from text and tool calls.
    content: list[dict[str, Any]]
    stop: Stop
    # Everything the request sent, cached or not.
    input_tokens: int = 0


class Model(Protocol):
    def respond(
        self,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> Reply: ...


class ModelUnavailable(Exception):
    """The model could not be reached: network, credentials, rate limit, a rejected request."""


def has_credentials() -> bool:
    """Whether the SDK finds credentials of its own: a key, a token or a login profile.

    The SDK builds a client without any and fails only at the first request, so
    the app asks here before reading any input. Nothing is sent.
    """
    import anthropic

    try:
        client = anthropic.Anthropic()
    except Exception:
        return False
    return bool(client.api_key or client.auth_token or client.credentials)


class AnthropicModel:
    """One tier's model over the Claude API. The client is made on first use."""

    def __init__(self, model_id: str, effort: str) -> None:
        self.model_id = model_id
        self.effort = effort
        self._client: Any = None
        # Token figures of every request, for checking that the cache is read.
        self.usage: list[dict[str, int]] = []

    def _messages_api(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        if self.model_id in FALLBACK_MODELS:
            return self._client.beta.messages
        return self._client.messages

    def request(
        self,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """The keyword arguments of one request. Separate so it can be checked offline."""
        marked = [dict(block) for block in system]
        if len(marked) > 1:
            # The first block is the stable part (preamble and guides); later ones vary.
            marked[0]["cache_control"] = {"type": "ephemeral"}
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": MAX_TOKENS,
            "system": marked,
            "tools": tools,
            "messages": messages,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.model_id in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def respond(
        self,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> Reply:
        import anthropic

        try:
            response = self._messages_api().create(**self.request(system, tools, messages))
        except anthropic.APIError as err:
            raise ModelUnavailable(str(err)) from err
        except TypeError as err:
            # How the SDK reports that no credentials could be resolved. Any other
            # TypeError is a bug and propagates.
            if "authentication" not in str(err):
                raise
            raise ModelUnavailable(str(err)) from err
        usage = response.usage
        figures = {
            "input": usage.input_tokens or 0,
            "cache_write": usage.cache_creation_input_tokens or 0,
            "cache_read": usage.cache_read_input_tokens or 0,
        }
        self.usage.append(figures)
        input_tokens = sum(figures.values())
        return Reply(
            content=[block.to_dict(mode="json") for block in response.content],
            stop=_STOPS.get(response.stop_reason or "", "end"),
            input_tokens=input_tokens,
        )
