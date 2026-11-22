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


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens. Cache writes are the 5-minute kind."""

    input: float
    output: float
    cache_read: float
    cache_write: float
    # Haiku 5.5 charges more for a prompt over this many tokens: every rate x5.
    long_above: int | None = None
    long_factor: float = 1.0


# For the estimate /usage shows (Claude API docs, 2026-11). Update when models
# or prices change; a model not listed gets token counts and no cost.
PRICES = {
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125, long_above=100_000, long_factor=5.0),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
}

Stop = Literal["end", "tool_use", "max_tokens", "refusal"]
_STOPS: dict[str, Stop] = {
    "end_turn": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


@dataclass(frozen=True)
class Usage:
    """The token figures of one request, as the API reports them."""

    input: int = 0  # not read from or written to the cache
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0

    @property
    def prompt(self) -> int:
        return self.input + self.cache_write + self.cache_read


def cost(model_id: str, usage: Usage) -> float | None:
    """Estimated US dollars for one request, or None for a model with no price."""
    price = PRICES.get(model_id)
    if price is None:
        return None
    factor = 1.0
    if price.long_above is not None and usage.prompt > price.long_above:
        factor = price.long_factor
    total = (
        usage.input * price.input
        + usage.cache_write * price.cache_write
        + usage.cache_read * price.cache_read
        + usage.output * price.output
    )
    return total * factor / 1_000_000


@dataclass(frozen=True)
class Reply:
    # The reply's blocks as plain dicts, opaque to the loop apart from text and tool calls.
    content: list[dict[str, Any]]
    stop: Stop
    # Everything the request sent, cached or not.
    input_tokens: int = 0
    # The full figures, when the model reports them.
    usage: Usage | None = None


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
        reported = response.usage
        usage = Usage(
            input=reported.input_tokens or 0,
            cache_write=reported.cache_creation_input_tokens or 0,
            cache_read=reported.cache_read_input_tokens or 0,
            output=reported.output_tokens or 0,
        )
        return Reply(
            content=[block.to_dict(mode="json") for block in response.content],
            stop=_STOPS.get(response.stop_reason or "", "end"),
            input_tokens=usage.prompt,
            usage=usage,
        )
