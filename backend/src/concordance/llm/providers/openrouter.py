"""OpenRouter. Second in the default chain.

OpenRouter is the fallback rather than the primary because it is a broker: a
free model behind it can be temporarily unavailable through no fault of the key,
and it signals that by returning 200 with an error object, which the shared
client already maps to `Transient`. As a fallback that behaviour is a feature -
the chain has already tried Groq by the time it matters.

It is also the provider that makes the production swap cheap. Changing
`LLM_MODEL` to a paid model id is the whole change; no code moves.
"""

from __future__ import annotations

from typing import Any

from concordance.llm.providers.openai_compat import OpenAICompatibleProvider

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "poolside/laguna-s-2.1:free"

# OpenRouter asks for these for attribution on its dashboards. They identify
# the project, carry nothing sensitive, and are safe in a request header.
DEFAULT_REFERER = "https://github.com/concordance/provider-reconciliation"
DEFAULT_TITLE = "Concordance"


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter's OpenAI-compatible endpoint."""

    name = "openrouter"
    base_url = OPENROUTER_BASE_URL
    supports_structured_output = True

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, **kwargs: Any) -> None:
        headers = {
            "HTTP-Referer": DEFAULT_REFERER,
            "X-Title": DEFAULT_TITLE,
            **(kwargs.pop("extra_headers", None) or {}),
        }
        super().__init__(api_key, model, extra_headers=headers, **kwargs)


__all__ = ["DEFAULT_MODEL", "OPENROUTER_BASE_URL", "OpenRouterProvider"]
