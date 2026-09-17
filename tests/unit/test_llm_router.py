"""Router policy: retry, then fail over, then give up - and never call twice for one prompt."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import pytest

from concordance.config import Settings
from concordance.llm.cache import FileCache, cache_key
from concordance.llm.errors import (
    AllProvidersFailed,
    AuthFailed,
    InvalidRequest,
    ProviderUnavailable,
    RateLimited,
    Transient,
)
from concordance.llm.prompt import rendered_prompt
from concordance.llm.providers.groq import DEFAULT_MODEL as GROQ_DEFAULT_MODEL
from concordance.llm.router import LLMRouter, backoff_delay, build_providers
from concordance.llm.types import ChatMessage, LLMResponse, user

pytestmark = pytest.mark.unit

MESSAGES = [user("adjudicate this")]


@dataclass
class FakeProvider:
    """Scripted provider. Each entry is either an exception to raise or content to return."""

    name: str = "fake"
    model: str = "fake-model"
    supports_structured_output: bool = False
    script: list[Any] = field(default_factory=list)
    calls: int = 0

    def complete(
        self, messages: list[ChatMessage], schema: dict[str, Any] | None = None, **opts: Any
    ) -> LLMResponse:
        self.calls += 1
        step = self.script[min(self.calls - 1, len(self.script) - 1)] if self.script else "{}"
        if isinstance(step, Exception):
            raise step
        return LLMResponse(
            content=step,
            provider=self.name,
            model=self.model,
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=12,
            cost_usd=0.0001,
        )


def router(*providers: FakeProvider, cache: Any = None, **kwargs: Any) -> LLMRouter:
    return LLMRouter(
        providers=list(providers),
        cache=cache,
        sleep=lambda _seconds: None,
        rng=random.Random(0),
        **kwargs,
    )


# -- the disabled paths ----------------------------------------------------


def test_disabled_router_refuses_rather_than_returning_a_fake_answer() -> None:
    engine = router(FakeProvider(), enabled=False)
    with pytest.raises(ProviderUnavailable):
        engine.complete(MESSAGES)
    assert engine.available is False


def test_an_empty_chain_refuses() -> None:
    engine = router()
    with pytest.raises(ProviderUnavailable):
        engine.complete(MESSAGES)


# -- retry -----------------------------------------------------------------


def test_a_transient_failure_is_retried_on_the_same_provider() -> None:
    provider = FakeProvider(script=[Transient("blip"), Transient("blip"), '{"ok": 1}'])
    engine = router(provider)
    response = engine.complete(MESSAGES)
    assert response.content == '{"ok": 1}'
    assert provider.calls == 3
    assert engine.stats.retries == 2
    assert engine.stats.failovers == 0


def test_retries_stop_at_max_attempts() -> None:
    provider = FakeProvider(script=[Transient("down")])
    engine = router(provider, max_attempts=2)
    with pytest.raises(AllProvidersFailed):
        engine.complete(MESSAGES)
    assert provider.calls == 2


@pytest.mark.parametrize("fatal", [AuthFailed("bad key"), InvalidRequest("bad model")])
def test_fatal_errors_are_not_retried(fatal: Exception) -> None:
    """A bad key does not become a good key after three seconds."""
    provider = FakeProvider(script=[fatal])
    engine = router(provider)
    with pytest.raises(AllProvidersFailed):
        engine.complete(MESSAGES)
    assert provider.calls == 1
    assert engine.stats.retries == 0


# -- failover --------------------------------------------------------------


def test_rate_limit_backs_off_then_fails_over() -> None:
    first = FakeProvider(name="groq", script=[RateLimited("429", retry_after=1.0)])
    second = FakeProvider(name="openrouter", script=['{"ok": 2}'])
    engine = router(first, second, max_attempts=3)
    response = engine.complete(MESSAGES)
    assert response.provider == "openrouter"
    assert first.calls == 3, "exhaust retries before moving on, not fail over on first error"
    assert second.calls == 1
    assert engine.stats.retries == 2
    assert engine.stats.failovers == 1


def test_a_dead_first_key_reaches_the_second_provider_immediately() -> None:
    """GATE 4's failover demo: kill the first provider's key, observe the chain."""
    first = FakeProvider(name="groq", script=[AuthFailed("invalid api key")])
    second = FakeProvider(name="openrouter", script=['{"ok": 3}'])
    engine = router(first, second)
    assert engine.complete(MESSAGES).provider == "openrouter"
    assert first.calls == 1
    assert engine.stats.failovers == 1


def test_every_provider_failing_names_each_failure() -> None:
    first = FakeProvider(name="groq", script=[AuthFailed("bad key")])
    second = FakeProvider(name="openrouter", script=[Transient("down")])
    engine = router(first, second, max_attempts=1)
    with pytest.raises(AllProvidersFailed) as caught:
        engine.complete(MESSAGES)
    assert set(caught.value.failures) == {"groq", "openrouter"}
    assert "bad key" in str(caught.value)


# -- caching ---------------------------------------------------------------


def test_second_identical_call_makes_zero_network_calls(tmp_path) -> None:
    """GATE 4 ⭐ - the cache-hit counter is the proof, not an absence of errors."""
    provider = FakeProvider(script=['{"ok": 4}'])
    cache = FileCache(tmp_path / "llm")

    first = router(provider, cache=cache)
    assert first.complete(MESSAGES).cached is False
    assert provider.calls == 1

    second = router(provider, cache=cache)
    response = second.complete(MESSAGES)
    assert response.cached is True
    assert response.content == '{"ok": 4}'
    assert provider.calls == 1, "a cached prompt must not reach the provider"
    assert second.stats.cache_hits == 1
    assert second.stats.calls == 0


def test_the_cache_key_is_the_documented_one(tmp_path) -> None:
    provider = FakeProvider(script=['{"ok": 5}'])
    cache = FileCache(tmp_path / "llm")
    engine = router(provider, cache=cache, prompt_version="adjudication_v1")
    engine.complete(MESSAGES)
    expected = cache_key(
        provider.name, provider.model, "adjudication_v1", rendered_prompt(MESSAGES)
    )
    assert cache.path_for(expected).exists()


def test_a_prompt_version_bump_invalidates_the_cache(tmp_path) -> None:
    provider = FakeProvider(script=['{"ok": 6}'])
    cache = FileCache(tmp_path / "llm")
    router(provider, cache=cache, prompt_version="v1").complete(MESSAGES)
    router(provider, cache=cache, prompt_version="v2").complete(MESSAGES)
    assert provider.calls == 2, "v2 must not reuse an answer produced by v1"


def test_failures_are_never_cached(tmp_path) -> None:
    provider = FakeProvider(script=[Transient("down")])
    cache = FileCache(tmp_path / "llm")
    engine = router(provider, cache=cache, max_attempts=1)
    with pytest.raises(AllProvidersFailed):
        engine.complete(MESSAGES)
    assert cache.entry_count() == 0


def test_a_cached_entry_carries_the_request_that_produced_it(tmp_path) -> None:
    provider = FakeProvider(script=['{"ok": 7}'])
    cache = FileCache(tmp_path / "llm")
    router(provider, cache=cache).complete(MESSAGES)
    entry = cache.entries()[0]
    assert entry["provider"] == "fake"
    assert entry["request"]["messages"][0]["content"] == "adjudicate this"
    assert entry["response"]["total_tokens"] == 15


# -- accounting ------------------------------------------------------------


def test_tokens_and_cost_accumulate_across_calls() -> None:
    provider = FakeProvider(script=['{"a": 1}'])
    engine = router(provider)
    engine.complete(MESSAGES)
    engine.complete([user("a different prompt")])
    stats = engine.stats_dict()
    assert stats["prompt_tokens"] == 20
    assert stats["completion_tokens"] == 10
    assert stats["tokens"] == 30
    assert stats["cost_usd"] == pytest.approx(0.0002)
    assert stats["by_provider"] == {"fake": 2}


def test_stats_report_the_chain_and_prompt_version() -> None:
    engine = router(FakeProvider(name="groq"), FakeProvider(name="openrouter"))
    stats = engine.stats_dict()
    assert stats["chain"] == ["groq", "openrouter"]
    assert stats["enabled"] is True
    assert "prompt_version" in stats


# -- backoff ---------------------------------------------------------------


def test_backoff_is_bounded_and_grows() -> None:
    rng = random.Random(1)
    ceilings = [max(backoff_delay(a, rng=rng) for _ in range(50)) for a in range(4)]
    assert ceilings == sorted(ceilings)
    assert all(d <= 8.0 for d in ceilings)


def test_backoff_is_jittered_so_a_batch_does_not_retry_in_lockstep() -> None:
    rng = random.Random(2)
    draws = {round(backoff_delay(2, rng=rng), 6) for _ in range(20)}
    assert len(draws) > 1


def test_a_provider_retry_after_hint_is_respected() -> None:
    rng = random.Random(3)
    assert backoff_delay(0, retry_after=5.0, rng=rng) == pytest.approx(5.0)


def test_retry_after_is_still_capped() -> None:
    assert backoff_delay(0, retry_after=3600.0, cap=8.0, rng=random.Random(4)) <= 8.0


# -- chain construction ----------------------------------------------------


def test_providers_without_a_key_are_skipped() -> None:
    settings = Settings(
        LLM_PROVIDER_CHAIN="groq,openrouter", GROQ_API_KEY="k", OPENROUTER_API_KEY=None
    )
    assert [p.name for p in build_providers(settings)] == ["groq"]


def test_chain_order_follows_the_setting() -> None:
    settings = Settings(
        LLM_PROVIDER_CHAIN="openrouter,groq", GROQ_API_KEY="k", OPENROUTER_API_KEY="k2"
    )
    assert [p.name for p in build_providers(settings)] == ["openrouter", "groq"]


def test_an_unknown_provider_name_is_skipped_not_fatal() -> None:
    settings = Settings(LLM_PROVIDER_CHAIN="groq,nonesuch", GROQ_API_KEY="k")
    assert [p.name for p in build_providers(settings)] == ["groq"]


def test_llm_model_overrides_every_provider_default() -> None:
    settings = Settings(
        LLM_PROVIDER_CHAIN="groq,openrouter",
        GROQ_API_KEY="k",
        OPENROUTER_API_KEY="k2",
        LLM_MODEL="pinned-model",
    )
    assert {p.model for p in build_providers(settings)} == {"pinned-model"}


def test_the_default_chain_leads_with_groq() -> None:
    assert Settings().LLM_PROVIDER_CHAIN.split(",")[0] == "groq"


def test_a_per_provider_model_applies_only_to_that_provider() -> None:
    """Two vendors share no model vocabulary, so one global pin cannot serve both."""
    settings = Settings(
        LLM_PROVIDER_CHAIN="groq,openrouter",
        GROQ_API_KEY="k",
        OPENROUTER_API_KEY="k2",
        OPENROUTER_MODEL="poolside/laguna-s-2.1:free",
    )
    models = {p.name: p.model for p in build_providers(settings)}
    assert models["openrouter"] == "poolside/laguna-s-2.1:free"
    assert models["groq"] == GROQ_DEFAULT_MODEL, "groq keeps its own default"


def test_a_per_provider_model_overrides_the_global_pin() -> None:
    settings = Settings(
        LLM_PROVIDER_CHAIN="groq,openrouter",
        GROQ_API_KEY="k",
        OPENROUTER_API_KEY="k2",
        LLM_MODEL="pinned-everywhere",
        OPENROUTER_MODEL="openrouter-only",
    )
    models = {p.name: p.model for p in build_providers(settings)}
    assert models["groq"] == "pinned-everywhere"
    assert models["openrouter"] == "openrouter-only"


def test_each_provider_gets_its_own_cache_namespace_via_the_model() -> None:
    """A per-provider model must not let one vendor's answer serve the other."""
    from concordance.llm.cache import cache_key

    settings = Settings(
        LLM_PROVIDER_CHAIN="groq,openrouter",
        GROQ_API_KEY="k",
        OPENROUTER_API_KEY="k2",
        OPENROUTER_MODEL="poolside/laguna-s-2.1:free",
    )
    keys = {cache_key(p.name, p.model, "v1", "prompt") for p in build_providers(settings)}
    assert len(keys) == 2


def test_the_completion_budget_reaches_every_provider() -> None:
    """A reasoning model spends most of `max_tokens` on hidden reasoning.

    Groq does not return a truncated answer when the budget runs out mid-object:
    it rejects the whole call with a 400 `json_validate_failed`, which reads like
    a prompt bug and is not one. The budget is therefore a setting rather than a
    client-local constant, and it has to arrive at the client.
    """
    settings = Settings(
        LLM_PROVIDER_CHAIN="groq,openrouter",
        GROQ_API_KEY="k",
        OPENROUTER_API_KEY="k2",
        LLM_MAX_TOKENS=4_096,
    )
    assert [p.max_tokens for p in build_providers(settings)] == [4_096, 4_096]


def test_the_ping_prompt_says_the_word_json() -> None:
    """Groq rejects a `json_object` response_format whose messages never say it.

    The ping prompt asks for `{"ok": true}`, which is JSON without containing the
    word, so the provider health check failed against a healthy provider until
    the word was added. Asserted on the prompt itself because the failure is a
    400 from the vendor, not something a fixture would catch.
    """
    from concordance.llm.router import PING_PROMPT

    assert "json" in PING_PROMPT.lower()
