"""The LLM layer: providers, router, cache, schema guard, adjudicator.

Import order matters here only in that nothing below `matching` may import
from this package - the engine works with no LLM at all, and that has to stay
true structurally rather than by habit. `matching.adjudication` defines the
seam; this package fills it.
"""

from concordance.llm.errors import (
    AllProvidersFailed,
    AuthFailed,
    InvalidRequest,
    LLMError,
    ProviderUnavailable,
    RateLimited,
    Transient,
)
from concordance.llm.types import ChatMessage, LLMProvider, LLMResponse

__all__ = [
    "AllProvidersFailed",
    "AuthFailed",
    "ChatMessage",
    "InvalidRequest",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "ProviderUnavailable",
    "RateLimited",
    "Transient",
]
