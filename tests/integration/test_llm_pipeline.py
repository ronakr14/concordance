"""The adjudicator inside the strategy, and the pipeline without one at all.

Two claims worth proving end to end rather than in isolation:

1. A grey-band record routed through `ProbabilisticLlmStrategy` reaches the
   adjudicator, and the adjudicator's answer changes the result's decision,
   route and reason - not just a note.
2. With `LLM_ENABLED=false` the identical pipeline runs and produces the
   unassisted engine's answers, with zero calls reported honestly. That is
   GATE 4's last line and the reason Stage 3's numbers stay reproducible on a
   machine with no API keys.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

import pytest

from concordance.config import Settings
from concordance.domain import Outcome
from concordance.eval.fitting import fit_config
from concordance.eval.pairs import prepare
from concordance.llm.ai_matcher import LlmAdjudicator, build_adjudicator
from concordance.llm.cache import FileCache
from concordance.llm.router import LLMRouter
from concordance.llm.types import ChatMessage, LLMResponse
from concordance.matching.scorer import DecisionReason, Route
from concordance.matching.strategies import StrategyName, build_strategy
from concordance.synth.pipeline import seed_dataset

SEED = 20260914
PROVIDERS = 4_000
SANCTIONS = 900

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("stage4")
    seed_dataset(
        providers=PROVIDERS,
        sanctions=SANCTIONS,
        corruption=0.5,
        seed=SEED,
        out_dir=out,
        write_excel=False,
    )
    return out


@pytest.fixture(scope="module")
def prepared_dataset(dataset):
    return prepare(dataset, max_candidates=30, show_progress=False)


@pytest.fixture(scope="module")
def fitted_engine(prepared_dataset):
    return fit_config(prepared_dataset, seed=SEED).config.engine()


@dataclass
class ScriptedProvider:
    name: str = "fake"
    model: str = "fake-model"
    supports_structured_output: bool = False
    reply: str = ""
    calls: int = 0
    last_prompt: str = field(default="", repr=False)

    def complete(
        self, messages: list[ChatMessage], schema: dict[str, Any] | None = None, **opts: Any
    ) -> LLMResponse:
        self.calls += 1
        self.last_prompt = "\n".join(m.content for m in messages)
        return LLMResponse(
            content=self.reply,
            provider=self.name,
            model=self.model,
            prompt_tokens=90,
            completion_tokens=30,
            cost_usd=0.0,
        )


def grey_band_case(engine: Any, prepared: Any) -> tuple[Any, Any]:
    """The first record the unassisted engine leaves AMBIGUOUS, with its work."""
    for work in prepared:
        result = engine.score_record(work.normalized, work.candidates)
        if result.decision is Outcome.AMBIGUOUS and result.candidates:
            return work, result
    pytest.skip("no grey-band record in the fixture dataset")


def adjudicator_answering(provider_id: str, cache: Any = None) -> tuple[LlmAdjudicator, Any]:
    reply = json.dumps(
        {
            "decision": "MATCH",
            "provider_id": provider_id,
            "confidence": 0.93,
            "evidence_cited": [],
            "reasoning": "agreement on the identifying fields is decisive",
        }
    )
    provider = ScriptedProvider(reply=reply)
    router = LLMRouter(
        providers=[provider], cache=cache, sleep=lambda _s: None, rng=random.Random(0)
    )
    return LlmAdjudicator(router=router), provider


def test_a_grey_band_record_reaches_the_adjudicator_and_its_answer_lands(
    fitted_engine: Any, prepared_dataset: Any
) -> None:
    work, baseline = grey_band_case(fitted_engine, prepared_dataset)
    chosen = baseline.candidates[0].provider_id
    judge, provider = adjudicator_answering(chosen)

    strategy = build_strategy(StrategyName.PROBABILISTIC_LLM, fitted_engine, judge)
    result = strategy.decide(work.normalized, work.candidates)

    assert provider.calls == 1
    assert result.decision is Outcome.MATCH
    assert result.route is Route.LLM
    assert result.reason is DecisionReason.ADJUDICATED
    assert result.chosen_provider_id == chosen
    assert result.confidence == pytest.approx(0.93)


def test_the_prompt_carries_no_raw_record_text(fitted_engine: Any, prepared_dataset: Any) -> None:
    """PLAN 7.8, proved against a real record rather than a hand-built one."""
    work, baseline = grey_band_case(fitted_engine, prepared_dataset)
    judge, provider = adjudicator_answering(baseline.candidates[0].provider_id)
    build_strategy(StrategyName.PROBABILISTIC_LLM, fitted_engine, judge).decide(
        work.normalized, work.candidates
    )

    prompt = provider.last_prompt
    record = work.record
    for value in (
        record.first_name,
        record.last_name,
        record.address_line1,
        record.organization_name,
    ):
        if value and len(str(value)) > 3:
            assert str(value).lower() not in prompt.lower()


def test_an_abstention_leaves_the_engines_answer_untouched(
    fitted_engine: Any, prepared_dataset: Any
) -> None:
    work, baseline = grey_band_case(fitted_engine, prepared_dataset)
    provider = ScriptedProvider(reply="I cannot answer that.")
    judge = LlmAdjudicator(
        router=LLMRouter(providers=[provider], cache=None, sleep=lambda _s: None)
    )
    strategy = build_strategy(StrategyName.PROBABILISTIC_LLM, fitted_engine, judge)
    result = strategy.decide(work.normalized, work.candidates)

    assert result.decision is baseline.decision
    assert result.chosen_provider_id == baseline.chosen_provider_id
    assert result.confidence == pytest.approx(baseline.confidence)


def test_re_running_the_same_record_makes_no_second_call(
    fitted_engine: Any, prepared_dataset: Any, tmp_path: Any
) -> None:
    work, baseline = grey_band_case(fitted_engine, prepared_dataset)
    cache = FileCache(tmp_path / "llm")
    chosen = baseline.candidates[0].provider_id

    judge, provider = adjudicator_answering(chosen, cache=cache)
    first = build_strategy(StrategyName.PROBABILISTIC_LLM, fitted_engine, judge).decide(
        work.normalized, work.candidates
    )
    assert provider.calls == 1

    second_judge = LlmAdjudicator(
        router=LLMRouter(providers=[provider], cache=cache, sleep=lambda _s: None)
    )
    second = build_strategy(StrategyName.PROBABILISTIC_LLM, fitted_engine, second_judge).decide(
        work.normalized, work.candidates
    )

    assert provider.calls == 1, "the second run must be served entirely from cache"
    assert second.decision is first.decision
    assert second.chosen_provider_id == first.chosen_provider_id


def test_the_whole_pipeline_runs_with_llm_disabled(
    fitted_engine: Any, prepared_dataset: Any
) -> None:
    """GATE 4 ⭐ - no keys, no network, honest zero in the stats."""
    settings = Settings(LLM_ENABLED=False, GROQ_API_KEY="k", OPENROUTER_API_KEY="k2")
    judge = build_adjudicator(settings)
    assert judge.name == "null"

    strategy = build_strategy(StrategyName.PROBABILISTIC_LLM, fitted_engine, judge)
    unassisted = build_strategy(StrategyName.PROBABILISTIC, fitted_engine)

    for work in prepared_dataset:
        with_llm = strategy.decide(work.normalized, work.candidates)
        without = unassisted.decide(work.normalized, work.candidates)
        assert with_llm.decision is without.decision
        assert with_llm.chosen_provider_id == without.chosen_provider_id

    stats = strategy.stats()
    assert stats["adjudicator"] == "null"
    assert stats["adjudicated"] == 0
    assert stats["tokens"] == 0
    assert stats["cost_usd"] == 0.0
