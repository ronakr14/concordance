"""The router: one chain, one cache, one place that counts what was spent.

Three policies live here, and each one is a decision rather than a default.

**Retry before failover, not instead of it.** A rate limit or a 503 is retried
against the same provider with jittered exponential backoff, because the same
provider a second later is usually the cheapest fix. Only when the retries are
exhausted does the chain move on. Failing over on the first error would burn
the fallback's quota on blips; retrying forever would hang a run. An
`AuthFailed` or `InvalidRequest` skips retries entirely and moves on at once -
a bad key does not heal.

**Jitter, not plain exponential.** When a batch of grey-band records hits a
rate limit together, an unjittered backoff retries them all at the same instant
and rate-limits them all again. The jitter is what breaks the convoy.

**Cache before network, per provider.** The key includes the provider and the
model, so the lookup happens inside the chain loop rather than before it. A run
that failed over to OpenRouter yesterday and finds Groq healthy today correctly
misses the cache and calls Groq - the cached answer belongs to a different
model and reusing it would misreport which model produced the decision.

`LLM_ENABLED=false` short-circuits everything. Not a stub that returns empty
text: `disabled` is a state callers check, so the whole pipeline runs with no
LLM configured at all and reports honestly that it did.
"""

from __future__ import annotations

import random
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from concordance.config import Settings
from concordance.llm.cache import FileCache, cache_key
from concordance.llm.errors import (
    AllProvidersFailed,
    AuthFailed,
    InvalidRequest,
    LLMError,
    ProviderUnavailable,
)
from concordance.llm.pricing import PriceTable
from concordance.llm.prompt import PROMPT_VERSION, rendered_prompt
from concordance.llm.providers.groq import GroqProvider
from concordance.llm.providers.openrouter import OpenRouterProvider
from concordance.llm.types import ChatMessage, LLMProvider, LLMResponse
from concordance.logging_setup import get_logger

log = get_logger("llm.router")

DEFAULT_MAX_ATTEMPTS = 3
#: The health-check prompt. The word "JSON" is load-bearing: Groq rejects a
#: `json_object` response_format whose messages never contain it, so a prompt
#: that merely shows JSON fails against a perfectly healthy provider.
PING_PROMPT = 'Reply with exactly this JSON and nothing else: {"ok": true}'
#: Generous because a reasoning model spends hidden tokens before the first
#: visible character of its answer.
PING_MAX_TOKENS = 256
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_BACKOFF_CAP = 8.0

#: Every provider this project knows how to construct, by the name used in
#: `LLM_PROVIDER_CHAIN`: the client class, its key setting, and its model
#: setting.
PROVIDER_REGISTRY: dict[str, tuple[type[Any], str, str]] = {
    "groq": (GroqProvider, "GROQ_API_KEY", "GROQ_MODEL"),
    "openrouter": (OpenRouterProvider, "OPENROUTER_API_KEY", "OPENROUTER_MODEL"),
}


def resolve_model(settings: Settings, model_field: str) -> str | None:
    """Which model this provider runs, or `None` to keep its class default.

    Specific beats general: a per-provider setting wins over the global
    `LLM_MODEL`. This is not a style preference - a chain spanning two vendors
    has no shared model vocabulary, so `LLM_MODEL` alone cannot express "Groq
    runs its model and OpenRouter runs a different one", and a global pin sends
    one vendor's model id to the other and earns a 404.

    `LLM_MODEL` therefore means "pin the whole chain", which is right when
    moving to production on a single model and wrong when running free models
    across both vendors.
    """
    specific = getattr(settings, model_field, None)
    return str(specific) if specific else (settings.LLM_MODEL or None)


@dataclass
class RouterStats:
    """What a run spent. Surfaced in the run summary and the eval report."""

    calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    retries: int = 0
    failovers: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)

    def record(self, response: LLMResponse) -> None:
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        self.cost_usd += response.cost_usd
        self.latency_ms += response.latency_ms
        self.by_provider[response.provider] = self.by_provider.get(response.provider, 0) + 1

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "retries": self.retries,
            "failovers": self.failovers,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 8),
            "latency_ms": self.latency_ms,
            "by_provider": dict(self.by_provider),
        }


def backoff_delay(
    attempt: int,
    *,
    base: float = DEFAULT_BACKOFF_BASE,
    cap: float = DEFAULT_BACKOFF_CAP,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Full-jitter exponential backoff, floored by the provider's own hint.

    `retry_after` wins when the provider sent one and it is longer than the
    computed delay: the provider knows its own window better than the formula
    does, and retrying earlier than it asked just earns another 429.
    """
    ceiling = min(cap, base * (2**attempt))
    picked = (rng or random).uniform(0.0, ceiling)
    if retry_after is not None:
        return max(picked, min(retry_after, cap))
    return picked


def build_providers(
    settings: Settings,
    *,
    prices: PriceTable | None = None,
    chain: Sequence[str] | None = None,
) -> list[LLMProvider]:
    """Construct the configured chain, skipping providers with no key.

    A missing key is a configuration fact, not an error: developing against one
    provider is normal. It is logged at info so a chain that silently shrank to
    one provider is visible in the run log rather than a surprise at failover.
    """
    table = prices or PriceTable.load(settings.LLM_PRICE_TABLE)
    names = (
        list(chain)
        if chain is not None
        else [n.strip().lower() for n in settings.LLM_PROVIDER_CHAIN.split(",") if n.strip()]
    )
    built: list[LLMProvider] = []
    for name in names:
        entry = PROVIDER_REGISTRY.get(name)
        if entry is None:
            log.warning("llm.router.unknown_provider", provider=name)
            continue
        cls, key_field, model_field = entry
        api_key = getattr(settings, key_field, None)
        if not api_key:
            log.info("llm.router.no_key", provider=name, setting=key_field)
            continue
        kwargs: dict[str, Any] = {
            "prices": table,
            "timeout": settings.LLM_TIMEOUT_SECONDS,
            "ca_bundle": settings.LLM_CA_BUNDLE,
            "max_tokens": settings.LLM_MAX_TOKENS,
        }
        model = resolve_model(settings, model_field)
        if model:
            kwargs["model"] = model
        built.append(cls(api_key, **kwargs))
    return built


@dataclass
class LLMRouter:
    """Sends one request down the chain and returns the first good answer."""

    providers: list[LLMProvider] = field(default_factory=list)
    cache: Any = None
    enabled: bool = True
    prompt_version: str = PROMPT_VERSION
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_cap: float = DEFAULT_BACKOFF_CAP
    sleep: Any = time.sleep
    rng: random.Random | None = None
    stats: RouterStats = field(default_factory=RouterStats)
    #: Replay mode. A cache miss raises rather than reaching a provider, so a
    #: replay that claims "every call served from cache" cannot quietly be
    #: wrong: the only way to produce a different answer is to fail loudly.
    offline: bool = False

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        cache: Any = None,
        providers: Sequence[LLMProvider] | None = None,
        offline: bool = False,
    ) -> LLMRouter:
        resolved_cache = cache if cache is not None else FileCache(settings.llm_cache_dir)
        return cls(
            providers=list(providers) if providers is not None else build_providers(settings),
            cache=resolved_cache,
            enabled=settings.LLM_ENABLED,
            max_attempts=settings.LLM_MAX_ATTEMPTS,
            offline=offline,
        )

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.providers)

    def chain_names(self) -> list[str]:
        return [p.name for p in self.providers]

    # -- the call ---------------------------------------------------------
    def complete(
        self,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        **opts: Any,
    ) -> LLMResponse:
        """First good answer from the chain, from cache when there is one.

        Raises `ProviderUnavailable` when disabled or unconfigured, and
        `AllProvidersFailed` when every provider was tried. Callers above
        translate both into an abstention; nothing here decides policy about
        what a failed adjudication means.
        """
        if not self.enabled:
            raise ProviderUnavailable("LLM_ENABLED is false")
        if not self.providers:
            raise ProviderUnavailable("no provider in the chain has an API key configured")

        prompt = rendered_prompt(messages)
        failures: dict[str, LLMError] = {}

        for index, provider in enumerate(self.providers):
            key = cache_key(provider.name, provider.model, self.prompt_version, prompt)
            cached = self._from_cache(key)
            if cached is not None:
                return cached

            if self.offline:
                failures[provider.name] = ProviderUnavailable(
                    f"offline: no cached answer for {provider.name}/{provider.model}"
                )
                continue

            try:
                response = self._call_with_retries(provider, messages, schema, key, **opts)
            except LLMError as exc:
                failures[provider.name] = exc
                if index + 1 < len(self.providers):
                    self.stats.failovers += 1
                    log.warning(
                        "llm.router.failover",
                        failed=provider.name,
                        next=self.providers[index + 1].name,
                        error=str(exc),
                    )
                continue
            return response

        self.stats.failures += 1
        raise AllProvidersFailed(dict(failures))

    def _from_cache(self, key: str) -> LLMResponse | None:
        if self.cache is None:
            return None
        entry = self.cache.get(key)
        if entry is None:
            self.stats.cache_misses += 1
            return None
        self.stats.cache_hits += 1
        log.debug("llm.router.cache_hit", key=key)
        return LLMResponse.from_dict(entry.get("response", {}), cached=True)

    def _call_with_retries(
        self,
        provider: LLMProvider,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None,
        key: str,
        **opts: Any,
    ) -> LLMResponse:
        last: LLMError | None = None
        for attempt in range(self.max_attempts):
            try:
                self.stats.calls += 1
                response = provider.complete(messages, schema, **opts)
            except (AuthFailed, InvalidRequest) as exc:
                # Not retryable by construction. Fail the provider immediately
                # so the chain reaches a healthy one without burning the budget.
                log.warning(
                    "llm.router.fatal",
                    provider=provider.name,
                    model=provider.model,
                    error=str(exc),
                )
                raise
            except LLMError as exc:
                last = exc
                if attempt + 1 >= self.max_attempts:
                    break
                delay = backoff_delay(
                    attempt,
                    base=self.backoff_base,
                    cap=self.backoff_cap,
                    retry_after=getattr(exc, "retry_after", None),
                    rng=self.rng,
                )
                self.stats.retries += 1
                log.warning(
                    "llm.router.retry",
                    provider=provider.name,
                    attempt=attempt + 1,
                    of=self.max_attempts,
                    delay=round(delay, 3),
                    error=str(exc),
                )
                self.sleep(delay)
                continue

            self.stats.record(response)
            log.info(
                "llm.router.call",
                provider=response.provider,
                model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                latency_ms=response.latency_ms,
                cost_usd=round(response.cost_usd, 8),
                prompt_version=self.prompt_version,
            )
            self._write_cache(key, provider, messages, response)
            return response

        assert last is not None
        raise last

    def _write_cache(
        self,
        key: str,
        provider: LLMProvider,
        messages: list[ChatMessage],
        response: LLMResponse,
    ) -> None:
        """Store a successful call, in the shape the Stage 5 `llm_calls` row needs."""
        if self.cache is None:
            return
        self.cache.put(
            key,
            {
                "key": key,
                "provider": provider.name,
                "model": response.model,
                "prompt_version": self.prompt_version,
                "request": {"messages": [m.as_dict() for m in messages]},
                "response": response.as_dict(),
                "created_at": time.time(),
            },
        )

    # -- diagnostics ------------------------------------------------------
    def ping(self) -> list[dict[str, Any]]:
        """One trivial completion per provider, reported per provider.

        Deliberately bypasses the chain: the point is to learn which providers
        are healthy, and failing over would hide exactly the answer being
        asked for.
        """
        from concordance.llm.types import user as user_message

        out: list[dict[str, Any]] = []
        for provider in self.providers:
            row: dict[str, Any] = {"provider": provider.name, "model": provider.model}
            started = time.perf_counter()
            try:
                response = provider.complete(
                    [user_message(PING_PROMPT)],
                    None,
                    max_tokens=PING_MAX_TOKENS,
                    json_mode=True,
                )
            except LLMError as exc:
                row.update(ok=False, error=str(exc), error_type=type(exc).__name__)
            else:
                row.update(
                    ok=True,
                    latency_ms=response.latency_ms,
                    tokens=response.total_tokens,
                    content=response.content.strip()[:120],
                )
            row["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
            out.append(row)
        return out

    def stats_dict(self) -> dict[str, Any]:
        payload = self.stats.as_dict()
        payload["chain"] = self.chain_names()
        payload["enabled"] = self.enabled
        payload["prompt_version"] = self.prompt_version
        if self.cache is not None:
            payload["cache"] = self.cache.stats()
        return payload

    def close(self) -> None:
        for provider in self.providers:
            close = getattr(provider, "close", None)
            if callable(close):
                close()


__all__ = [
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_BACKOFF_CAP",
    "DEFAULT_MAX_ATTEMPTS",
    "PROVIDER_REGISTRY",
    "LLMRouter",
    "RouterStats",
    "backoff_delay",
    "build_providers",
    "resolve_model",
]
