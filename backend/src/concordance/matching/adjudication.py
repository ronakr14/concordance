"""The grey-band seam, and the adjudicator that does nothing.

Stage 4 puts an LLM here. Stage 3 needs the shape of the hole now, for two
reasons. The evaluation sweep has a `probabilistic_llm` cell in it, and a cell
that cannot run is a hole in the robustness curve; and the request object is
where the anti-hallucination rule lives, so it is better fixed before there is
a model to tempt anyone into relaxing it.

The rule, from PLAN 7.8: an adjudicator receives **normalized evidence and
field scores only** - never raw free text from the source file. `AdjudicationRequest`
is therefore built from the comparison vector and the agreement levels, not from
the `SanctionRecord`, and `supplied_evidence()` enumerates exactly what was sent
so a response citing anything else can be rejected mechanically rather than by
eye.

`NullAdjudicator` abstains on everything. The cell completes, the numbers are
honest, `llm_calls` is zero, and the report labels the strategy with the
adjudicator that actually ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from concordance.domain import Outcome
from concordance.matching.comparators import ModelKind
from concordance.matching.scorer import MatchResult, ScoredCandidate

PROMPT_VERSION_NONE = "none"


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """One candidate, as an adjudicator is allowed to see it."""

    provider_id: str
    rank: int
    levels: dict[str, str]
    field_weights: dict[str, float]
    match_weight: float
    confidence: float

    @classmethod
    def of(cls, candidate: ScoredCandidate) -> CandidateEvidence:
        return cls(
            provider_id=candidate.provider_id,
            rank=candidate.rank,
            levels=dict(candidate.levels),
            field_weights={str(f["field"]): float(f["weight"]) for f in candidate.field_weights},
            match_weight=candidate.match_weight,
            confidence=candidate.confidence,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "rank": self.rank,
            "levels": dict(self.levels),
            "field_weights": {k: round(v, 4) for k, v in self.field_weights.items()},
            "match_weight": round(self.match_weight, 4),
            "confidence": round(self.confidence, 6),
        }


@dataclass(frozen=True, slots=True)
class AdjudicationRequest:
    """Everything an adjudicator gets, and nothing else.

    Deliberately not a `SanctionRecord`. Free text from the source file never
    reaches the model - not the raw name, not the address line, not the
    sanction narrative - because identity resolution is a decision about
    agreement levels, and a model given the narrative starts reasoning about
    the misconduct instead.
    """

    record_id: str
    kind: ModelKind
    candidates: tuple[CandidateEvidence, ...]
    grey_band: tuple[float, float]

    def supplied_evidence(self) -> frozenset[str]:
        """Every `provider_id.field` an answer is permitted to cite.

        A response naming anything outside this set invented it, and the
        response is rejected on that basis alone - a cheap, mechanical
        anti-hallucination check that needs no second model to run.
        """
        return frozenset(
            f"{c.provider_id}.{field_name}" for c in self.candidates for field_name in c.levels
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "model": str(self.kind),
            "grey_band": [round(v, 6) for v in self.grey_band],
            "candidates": [c.as_dict() for c in self.candidates],
        }

    @classmethod
    def of(
        cls, result: MatchResult, grey_band: tuple[float, float], top_k: int = 3
    ) -> AdjudicationRequest:
        return cls(
            record_id=result.record_id,
            kind=result.kind,
            candidates=tuple(CandidateEvidence.of(c) for c in result.candidates[:top_k]),
            grey_band=grey_band,
        )


@dataclass(frozen=True, slots=True)
class AdjudicationOutcome:
    """An adjudicator's answer. Abstaining is a legitimate answer, not a failure."""

    decision: Outcome
    provider_id: str | None = None
    confidence: float | None = None
    evidence_cited: tuple[str, ...] = ()
    reasoning: str = ""
    abstained: bool = False
    prompt_version: str = PROMPT_VERSION_NONE
    tokens: int = 0
    cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "provider_id": self.provider_id,
            "confidence": self.confidence,
            "evidence_cited": list(self.evidence_cited),
            "reasoning": self.reasoning,
            "abstained": self.abstained,
            "prompt_version": self.prompt_version,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass
class NullAdjudicator:
    """Abstains on every pair. The Stage 3 stand-in for the Stage 4 LLM.

    It exists so the `probabilistic_llm` sweep cell runs to completion with
    honest numbers rather than being skipped: every grey-band record stays
    `AMBIGUOUS`, the call count is zero, and the report names the adjudicator
    that ran so nobody mistakes the curve for an LLM result.
    """

    name: str = "null"
    calls: int = 0
    abstentions: int = 0
    _unused: dict[str, Any] = field(default_factory=dict, repr=False)

    def adjudicate(self, request: AdjudicationRequest) -> AdjudicationOutcome:
        self.calls += 1
        self.abstentions += 1
        return AdjudicationOutcome(
            decision=Outcome.AMBIGUOUS,
            provider_id=request.candidates[0].provider_id if request.candidates else None,
            abstained=True,
            reasoning="no adjudicator configured; grey band left for human review",
        )

    def stats(self) -> dict[str, Any]:
        return {
            "adjudicator": self.name,
            "calls": self.calls,
            "adjudicated": self.calls - self.abstentions,
            "abstentions": self.abstentions,
            "tokens": 0,
            "cost_usd": 0.0,
        }
