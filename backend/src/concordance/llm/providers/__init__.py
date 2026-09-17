"""Provider clients. Both are the same OpenAI-compatible client, differently configured."""

from concordance.llm.providers.groq import GroqProvider
from concordance.llm.providers.openai_compat import OpenAICompatibleProvider
from concordance.llm.providers.openrouter import OpenRouterProvider

__all__ = ["GroqProvider", "OpenAICompatibleProvider", "OpenRouterProvider"]
