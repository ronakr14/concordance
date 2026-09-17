"""The provider contract: what goes in, what comes back.

`LLMResponse` is deliberately flat and provider-neutral. Every field on it is
something the Stage 5 `llm_calls` table needs, which is the point: a cache
entry written today is a database row tomorrow, and the migration is an import
rather than a translation.

`raw` keeps the provider's own JSON body. It is never parsed by anything above
this layer - it exists so a surprising answer can be investigated months later
without re-running the call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn. Roles are the OpenAI-compatible three; both providers take them."""

    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


def system(content: str) -> ChatMessage:
    return ChatMessage(ROLE_SYSTEM, content)


def user(content: str) -> ChatMessage:
    return ChatMessage(ROLE_USER, content)


def assistant(content: str) -> ChatMessage:
    return ChatMessage(ROLE_ASSISTANT, content)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A normalized completion. Same shape from every provider."""

    content: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    finish_reason: str = ""
    cached: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        """Everything a `llm_calls` row needs, and nothing that is not serializable."""
        return {
            "content": self.content,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "cost_usd": round(self.cost_usd, 8),
            "finish_reason": self.finish_reason,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, cached: bool = False) -> LLMResponse:
        return cls(
            content=str(payload.get("content", "")),
            provider=str(payload.get("provider", "")),
            model=str(payload.get("model", "")),
            prompt_tokens=int(payload.get("prompt_tokens", 0)),
            completion_tokens=int(payload.get("completion_tokens", 0)),
            latency_ms=int(payload.get("latency_ms", 0)),
            cost_usd=float(payload.get("cost_usd", 0.0)),
            finish_reason=str(payload.get("finish_reason", "")),
            cached=cached,
            raw=dict(payload.get("raw", {})),
        )


@runtime_checkable
class LLMProvider(Protocol):
    """One vendor endpoint, already bound to a model and a key."""

    name: str
    model: str
    supports_structured_output: bool

    def complete(
        self,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        **opts: Any,
    ) -> LLMResponse:
        """Run one completion, or raise something from `llm.errors`.

        `schema` is advisory. A provider advertising `supports_structured_output`
        may use it to constrain decoding; the router does not currently let it
        (PLAN 11.6), so the prompt-based JSON path stays the tested path rather
        than becoming dead code that only runs in production.
        """
        ...


__all__ = [
    "ROLE_ASSISTANT",
    "ROLE_SYSTEM",
    "ROLE_USER",
    "ChatMessage",
    "LLMProvider",
    "LLMResponse",
    "assistant",
    "system",
    "user",
]
