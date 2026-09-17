"""Groq. First in the default chain.

Chosen as the primary because its free tier is fast enough that grey-band
adjudication does not dominate a reconciliation run's wall time, and because
its rate limits are per-minute rather than per-day - a limit a backoff can wait
out rather than one that ends the run.

`supports_structured_output` is true and deliberately unused: see
`OpenAICompatibleProvider` and PLAN 11.6.
"""

from __future__ import annotations

from typing import Any

from concordance.llm.providers.openai_compat import OpenAICompatibleProvider

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"


class GroqProvider(OpenAICompatibleProvider):
    """Groq's OpenAI-compatible endpoint."""

    name = "groq"
    base_url = GROQ_BASE_URL
    supports_structured_output = True

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, **kwargs: Any) -> None:
        super().__init__(api_key, model, **kwargs)


__all__ = ["DEFAULT_MODEL", "GROQ_BASE_URL", "GroqProvider"]
