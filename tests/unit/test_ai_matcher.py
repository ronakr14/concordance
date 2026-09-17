"""The adjudicator: what it accepts, what it refuses, and where it can never go.

The asymmetry is the thing under test. The adjudicator may promote a grey-band
record to `MATCH`; it may never remove one from human review. Every failure path
- disabled, unconfigured, dead providers, malformed output twice, invented
evidence - has to land on the same abstention.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

import pytest

from concordance.config import Settings
from concordance.domain import Outcome
from concordance.llm.ai_matcher import LlmAdjudicator, build_adjudicator
from concordance.llm.cache import FileCache
from concordance.llm.errors import AuthFailed, Transient
from concordance.llm.router import LLMRouter
from concordance.llm.schema import Rejection
from concordance.llm.types import ChatMessage, LLMResponse
from concordance.matching.adjudication import AdjudicationRequest, CandidateEvidence
from concordance.matching.comparators import ModelKind

pytestmark = pytest.mark.unit


@dataclass
class ScriptedProvider:
    name: str = "fake"
    model: str = "fake-model"
    supports_structured_output: bool = False
    script: list[Any] = field(default_factory=list)
    calls: int = 0
    seen: list[list[ChatMessage]] = field(default_factory=list)

    def complete(
        self, messages: list[ChatMessage], schema: dict[str, Any] | None = None, **opts: Any
    ) -> LLMResponse:
        self.calls += 1
        self.seen.append(list(messages))
        step = self.script[min(self.calls - 1, len(self.script) - 1)] if self.script else "{}"
        if isinstance(step, Exception):
            raise step
        return LLMResponse(
            content=step,
            provider=self.name,
            model=self.model,
            prompt_tokens=100,
            completion_tokens=20,
            latency_ms=5,
            cost_usd=0.0,
        )


def adjudicator(*script: Any, cache: Any = None) -> tuple[LlmAdjudicator, ScriptedProvider]:
    provider = ScriptedProvider(script=list(script))
    router = LLMRouter(
        providers=[provider],
        cache=cache,
        sleep=lambda _s: None,
        rng=random.Random(0),
    )
    return LlmAdjudicator(router=router), provider


def request(*, candidates: int = 2) -> AdjudicationRequest:
    return AdjudicationRequest(
        record_id="REC-1",
        kind=ModelKind.INDIVIDUAL,
        candidates=tuple(
            CandidateEvidence(
                provider_id=f"PRV-{i + 1}",
                rank=i + 1,
                levels={"last_name": "EXACT", "dob": "MISSING"},
                field_weights={"last_name": 4.0, "dob": 0.0},
                match_weight=3.0 - i,
                confidence=0.6 - 0.05 * i,
            )
            for i in range(candidates)
        ),
        grey_band=(0.42, 0.91),
    )


def answer(**overrides: Any) -> str:
    payload = {
        "decision": "MATCH",
        "provider_id": "PRV-1",
        "confidence": 0.87,
        "evidence_cited": ["PRV-1.last_name"],
        "reasoning": "exact agreement on last name",
    }
    payload.update(overrides)
    return json.dumps(payload)


# -- the happy path --------------------------------------------------------


def test_a_valid_match_is_accepted() -> None:
    judge, provider = adjudicator(answer())
    outcome = judge.adjudicate(request())
    assert outcome.decision is Outcome.MATCH
    assert outcome.provider_id == "PRV-1"
    assert outcome.confidence == pytest.approx(0.87)
    assert outcome.abstained is False
    assert outcome.evidence_cited == ("PRV-1.last_name",)
    assert outcome.prompt_version == judge.prompt_version
    assert provider.calls == 1


def test_the_outcome_carries_tokens_and_cost_for_the_run_summary() -> None:
    judge, _ = adjudicator(answer())
    outcome = judge.adjudicate(request())
    assert outcome.tokens == 120
    stats = judge.stats()
    assert stats["tokens"] == 120
    assert "cost_usd" in stats and "calls" in stats


def test_a_response_wrapped_in_prose_is_still_accepted() -> None:
    judge, _ = adjudicator(f"Here is my answer:\n```json\n{answer()}\n```")
    assert judge.adjudicate(request()).decision is Outcome.MATCH


# -- the conservative mapping ----------------------------------------------


def test_no_confident_match_becomes_ambiguous_not_no_match() -> None:
    """The model may never close a record out of the review queue."""
    judge, _ = adjudicator(
        answer(decision="NO_CONFIDENT_MATCH", provider_id=None, confidence=0.2, evidence_cited=[])
    )
    outcome = judge.adjudicate(request())
    assert outcome.decision is Outcome.AMBIGUOUS
    assert outcome.abstained is False, "a considered refusal is a decision, not an abstention"


def test_ambiguous_stays_ambiguous() -> None:
    judge, _ = adjudicator(
        answer(decision="AMBIGUOUS", provider_id=None, confidence=0.5, evidence_cited=[])
    )
    assert judge.adjudicate(request()).decision is Outcome.AMBIGUOUS


def test_no_decision_path_can_produce_no_match() -> None:
    for decision in ("MATCH", "NO_CONFIDENT_MATCH", "AMBIGUOUS"):
        pid = "PRV-1" if decision == "MATCH" else None
        judge, _ = adjudicator(
            answer(decision=decision, provider_id=pid, evidence_cited=["PRV-1.last_name"])
        )
        assert judge.adjudicate(request()).decision is not Outcome.NO_MATCH


# -- repair ----------------------------------------------------------------


def test_malformed_then_repaired_succeeds() -> None:
    judge, provider = adjudicator("I'm not sure I can answer that.", answer())
    outcome = judge.adjudicate(request())
    assert outcome.decision is Outcome.MATCH
    assert provider.calls == 2
    assert judge.counters.repairs == 1
    assert "repair" in outcome.reasoning


def test_the_repair_turn_quotes_the_rejected_output_and_the_reason() -> None:
    judge, provider = adjudicator("garbage", answer())
    judge.adjudicate(request())
    repair_turn = provider.seen[1][-1].content
    assert "garbage" in repair_turn
    assert Rejection.NO_JSON in repair_turn


def test_malformed_twice_degrades_to_ambiguous_without_raising() -> None:
    """GATE 4 ⭐ - a bad model day is a record for a human, not an outage."""
    judge, provider = adjudicator("nonsense", "still nonsense")
    outcome = judge.adjudicate(request())
    assert outcome.decision is Outcome.AMBIGUOUS
    assert outcome.abstained is True
    assert provider.calls == 2, "exactly one repair round, never a loop"
    assert judge.counters.rejections[Rejection.NO_JSON] == 1


def test_repair_is_attempted_once_even_for_repeated_schema_failures() -> None:
    judge, provider = adjudicator(json.dumps({"decision": "MATCH"}), json.dumps({"decision": "X"}))
    assert judge.adjudicate(request()).abstained is True
    assert provider.calls == 2


# -- the guards ------------------------------------------------------------


def test_citing_unsupplied_evidence_is_rejected() -> None:
    """GATE 4 ⭐ - invented justification voids the decision it justified."""
    judge, _ = adjudicator(
        answer(evidence_cited=["PRV-1.state_medical_board_record"]),
        answer(evidence_cited=["PRV-1.state_medical_board_record"]),
    )
    outcome = judge.adjudicate(request())
    assert outcome.decision is Outcome.AMBIGUOUS
    assert outcome.abstained is True
    assert judge.counters.rejections[Rejection.UNSUPPLIED_EVIDENCE] == 1
    assert "state_medical_board_record" in outcome.reasoning


def test_naming_a_provider_outside_the_candidate_set_is_rejected() -> None:
    judge, _ = adjudicator(answer(provider_id="PRV-999"), answer(provider_id="PRV-999"))
    outcome = judge.adjudicate(request())
    assert outcome.abstained is True
    assert judge.counters.rejections[Rejection.UNKNOWN_PROVIDER] == 1


def test_an_out_of_range_confidence_is_rejected() -> None:
    judge, _ = adjudicator(answer(confidence=7.0), answer(confidence=7.0))
    assert judge.adjudicate(request()).abstained is True


def test_a_rejected_response_never_names_a_provider_as_the_answer() -> None:
    judge, _ = adjudicator(answer(provider_id="PRV-999"), answer(provider_id="PRV-999"))
    outcome = judge.adjudicate(request())
    assert outcome.decision is Outcome.AMBIGUOUS
    assert outcome.confidence is None


# -- failure paths ---------------------------------------------------------


def test_an_unavailable_router_abstains_without_calling() -> None:
    judge = LlmAdjudicator(router=LLMRouter(providers=[], cache=None, enabled=False))
    outcome = judge.adjudicate(request())
    assert outcome.abstained is True
    assert outcome.decision is Outcome.AMBIGUOUS
    assert judge.counters.abstentions == 1


def test_every_provider_failing_abstains_rather_than_raising() -> None:
    judge, _ = adjudicator(Transient("down"))
    judge.router.max_attempts = 1
    outcome = judge.adjudicate(request())
    assert outcome.abstained is True
    assert "failed" in outcome.reasoning


def test_a_dead_key_abstains_rather_than_raising() -> None:
    judge, _ = adjudicator(AuthFailed("invalid api key"))
    assert judge.adjudicate(request()).abstained is True


def test_a_request_with_no_candidates_abstains_without_calling() -> None:
    judge, provider = adjudicator(answer())
    empty = AdjudicationRequest(
        record_id="REC-2", kind=ModelKind.INDIVIDUAL, candidates=(), grey_band=(0.4, 0.9)
    )
    assert judge.adjudicate(empty).abstained is True
    assert provider.calls == 0


# -- caching end to end ----------------------------------------------------


def test_re_adjudicating_the_same_record_makes_zero_network_calls(tmp_path) -> None:
    """GATE 4 ⭐ - proved by the call counter, through the adjudicator rather than the router."""
    cache = FileCache(tmp_path / "llm")
    first, provider = adjudicator(answer(), cache=cache)
    assert first.adjudicate(request()).decision is Outcome.MATCH
    assert provider.calls == 1

    second = LlmAdjudicator(
        router=LLMRouter(providers=[provider], cache=cache, sleep=lambda _s: None)
    )
    outcome = second.adjudicate(request())
    assert outcome.decision is Outcome.MATCH
    assert provider.calls == 1
    assert second.stats()["router"]["cache_hits"] == 1


# -- construction ----------------------------------------------------------


def test_disabled_settings_yield_the_null_adjudicator() -> None:
    judge = build_adjudicator(Settings(LLM_ENABLED=False, GROQ_API_KEY="k"))
    assert judge.name == "null"
    assert judge.adjudicate(request()).abstained is True


def test_enabled_but_keyless_settings_yield_the_null_adjudicator() -> None:
    judge = build_adjudicator(
        Settings(LLM_ENABLED=True, GROQ_API_KEY=None, OPENROUTER_API_KEY=None)
    )
    assert judge.name == "null"


def test_enabled_and_keyed_settings_yield_a_real_adjudicator() -> None:
    judge = build_adjudicator(Settings(LLM_ENABLED=True, GROQ_API_KEY="k"))
    assert judge.name == "llm"
    assert judge.router.chain_names() == ["groq"]
    judge.router.close()
