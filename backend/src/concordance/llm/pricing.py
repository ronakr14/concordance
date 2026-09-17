"""Per-token prices, and the cost of one call.

Costs are computed from a table, not from whatever the provider happens to
report, for two reasons. Free-tier responses carry no cost field at all, so
there would be nothing to record; and the number that matters for the Lab
page's cost-versus-baseline panel is "what would this run have cost on the
production model", which is a question about a model we are not currently
calling.

So every run records a cost even when the call was free, the table is
overridable from disk via `LLM_PRICE_TABLE`, and an unknown model costs zero
with a warning rather than raising - a missing price is a reporting gap, not a
reason to fail a reconciliation run.

**The numbers below are defaults, not quotes.** Provider pricing changes
without notice; verify against the provider's current pricing page before any
cost figure leaves this project. The free models are the ones the system is
developed against and they are genuinely 0.00.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from concordance.logging_setup import get_logger

log = get_logger("llm.pricing")

# USD per 1,000,000 tokens.
PER_MILLION = 1_000_000.0


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Input and output price per million tokens, plus the context window.

    `context_window` lives here rather than in the provider client because the
    prompt budget is a property of the model, and the budget check has to be
    answerable before a provider is even selected.
    """

    prompt_per_million: float
    completion_per_million: float
    context_window: int
    free: bool = False

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.prompt_per_million
            + completion_tokens * self.completion_per_million
        ) / PER_MILLION

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_per_million": self.prompt_per_million,
            "completion_per_million": self.completion_per_million,
            "context_window": self.context_window,
            "free": self.free,
        }


# Development models: free tiers on both providers. These are what the system
# is built and evaluated against.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    # --- Groq free tier ---------------------------------------------------
    "openai/gpt-oss-20b": ModelPrice(0.0, 0.0, 131_072, free=True),
    "openai/gpt-oss-120b": ModelPrice(0.0, 0.0, 131_072, free=True),
    "groq/compound-mini": ModelPrice(0.0, 0.0, 131_072, free=True),
    # --- OpenRouter free tier --------------------------------------------
    "poolside/laguna-s-2.1:free": ModelPrice(0.0, 0.0, 262_144, free=True),
    "poolside/laguna-xs-2.1:free": ModelPrice(0.0, 0.0, 262_144, free=True),
    # --- paid, for the production cost projection only --------------------
    # Placeholders. Verify before quoting: see the module docstring.
    "meta-llama/llama-3.3-70b-instruct": ModelPrice(0.10, 0.32, 131_072),
    "qwen/qwen-2.5-72b-instruct": ModelPrice(0.36, 0.40, 32_768),
}

UNKNOWN_MODEL = ModelPrice(0.0, 0.0, 8_192)


@dataclass
class PriceTable:
    """The prices this process is running with."""

    prices: dict[str, ModelPrice]

    @classmethod
    def default(cls) -> PriceTable:
        return cls(dict(DEFAULT_PRICES))

    @classmethod
    def load(cls, path: Path | str | None) -> PriceTable:
        """Defaults, overlaid with a JSON file if one was configured."""
        table = cls.default()
        if path is None:
            return table
        source = Path(path)
        if not source.exists():
            log.warning("llm.pricing.missing_file", path=str(source))
            return table
        payload = json.loads(source.read_text(encoding="utf-8"))
        for model, spec in payload.items():
            table.prices[model] = ModelPrice(
                prompt_per_million=float(spec.get("prompt_per_million", 0.0)),
                completion_per_million=float(spec.get("completion_per_million", 0.0)),
                context_window=int(spec.get("context_window", UNKNOWN_MODEL.context_window)),
                free=bool(spec.get("free", False)),
            )
        log.info("llm.pricing.loaded", path=str(source), models=len(payload))
        return table

    def get(self, model: str) -> ModelPrice:
        price = self.prices.get(model)
        if price is None:
            log.warning("llm.pricing.unknown_model", model=model)
            return UNKNOWN_MODEL
        return price

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        return self.get(model).cost(prompt_tokens, completion_tokens)

    def context_window(self, model: str) -> int:
        return self.get(model).context_window


__all__ = ["DEFAULT_PRICES", "UNKNOWN_MODEL", "ModelPrice", "PriceTable"]
