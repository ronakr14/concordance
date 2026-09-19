"""The four strategies the sweep compares.

They exist so the probabilistic engine has something to beat, and so the claim
"learned weights beat hand-tuned ones" is a measurement rather than an
assertion. Each takes the same input and returns the same `MatchResult`, so the
evaluation harness does not know or care which one it is running.

- `deterministic` - identifier matching only. A valid NPI that agrees is a
  match; everything else is a non-match. Perfect precision on the
  `exact_npi` scenario and hopeless everywhere else, which is the point: most
  exclusion records have no usable NPI at all.
- `fuzzy` - the matcher most people build first. Weighted RapidFuzz similarity
  over the normalized fields, hand-picked weights, two hand-picked thresholds.
  It is a genuinely reasonable baseline and it has two structural weaknesses
  the sweep is designed to expose: a missing field and a contradicting field
  both score zero, and the weights are a guess that no amount of data corrects.
- `probabilistic` - the Fellegi-Sunter engine with EM-learned weights, isotonic
  calibration and thresholds chosen for a precision target.
- `probabilistic_llm` - the same, with the grey band handed to an adjudicator.
  Before Stage 4 the adjudicator abstains, so the cell runs, reports zero calls
  and matches the `probabilistic` numbers exactly. That equality is itself
  worth seeing: it proves the LLM stage is additive rather than load-bearing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from concordance.domain import Candidate, Outcome
from concordance.matching.adjudication import AdjudicationRequest, NullAdjudicator
from concordance.matching.comparators import ModelKind, jaro_winkler, token_set
from concordance.matching.normalization import NormalizedRecord
from concordance.matching.npi_validator import NpiStatus
from concordance.matching.scorer import (
    DecisionReason,
    MatchingEngine,
    MatchResult,
    Route,
    ScoredCandidate,
)


class StrategyName(StrEnum):
    DETERMINISTIC = "deterministic"
    FUZZY = "fuzzy"
    PROBABILISTIC = "probabilistic"
    PROBABILISTIC_LLM = "probabilistic_llm"


ALL_STRATEGIES: tuple[StrategyName, ...] = tuple(StrategyName)

CandidatePair = tuple[NormalizedRecord, Candidate]


class Strategy(Protocol):
    """One way of turning a record and its candidates into a decision."""

    name: StrategyName

    def decide(
        self, record: NormalizedRecord, candidates: Sequence[CandidatePair]
    ) -> MatchResult: ...

    def stats(self) -> dict[str, Any]: ...


def _empty(record: NormalizedRecord, kind: ModelKind) -> MatchResult:
    return MatchResult(
        record_id=record.record_id,
        decision=Outcome.NO_MATCH,
        route=Route.NO_CANDIDATES,
        reason=DecisionReason.NO_CANDIDATES,
        kind=kind,
    )


def _kind_of(record: NormalizedRecord) -> ModelKind:
    return ModelKind.ORGANIZATION if record.is_organization else ModelKind.INDIVIDUAL


# --------------------------------------------------------------------------
# deterministic
# --------------------------------------------------------------------------


@dataclass
class DeterministicStrategy:
    """Valid NPI, exact agreement, nothing else. The floor."""

    name: StrategyName = StrategyName.DETERMINISTIC

    def decide(self, record: NormalizedRecord, candidates: Sequence[CandidatePair]) -> MatchResult:
        kind = _kind_of(record)
        if not candidates:
            return _empty(record, kind)

        hits = [
            provider
            for provider, _ in candidates
            if record.npi_status is NpiStatus.VALID
            and provider.npi_status is NpiStatus.VALID
            and record.npi == provider.npi
        ]
        scored = tuple(
            ScoredCandidate(
                provider_id=provider.record_id,
                rank=i + 1,
                kind=kind,
                vector=(),
                levels={"npi": "VALID_EXACT"},
                match_weight=0.0,
                posterior=1.0,
                confidence=1.0,
                field_weights=(),
                blocking_keys=(),
                npi_exact=True,
            )
            for i, provider in enumerate(hits[:1])
        )
        if not hits:
            return MatchResult(
                record_id=record.record_id,
                decision=Outcome.NO_MATCH,
                route=Route.DETERMINISTIC,
                reason=DecisionReason.BELOW_REJECT,
                kind=kind,
                notes=("no valid NPI agreement",),
            )
        if len(hits) > 1:
            return MatchResult(
                record_id=record.record_id,
                decision=Outcome.AMBIGUOUS,
                route=Route.DETERMINISTIC,
                reason=DecisionReason.NPI_EXACT_CONFLICT,
                kind=kind,
                chosen_provider_id=hits[0].record_id,
                confidence=1.0,
                candidates=scored,
                notes=("more than one provider carries this NPI in the master",),
            )
        return MatchResult(
            record_id=record.record_id,
            decision=Outcome.MATCH,
            route=Route.DETERMINISTIC,
            reason=DecisionReason.NPI_EXACT,
            kind=kind,
            chosen_provider_id=hits[0].record_id,
            confidence=1.0,
            candidates=scored,
        )

    def stats(self) -> dict[str, Any]:
        return {"strategy": str(self.name)}


# --------------------------------------------------------------------------
# fuzzy
# --------------------------------------------------------------------------

# Hand-picked, exactly as they would be in a first implementation - which is
# the honest form of the baseline. They sum to 1.0 and nothing in the system
# ever updates them.
FUZZY_WEIGHTS_INDIVIDUAL: dict[str, float] = {
    "npi": 0.30,
    "name": 0.30,
    "dob": 0.15,
    "address": 0.10,
    "zip": 0.05,
    "state": 0.05,
    "license": 0.05,
}
FUZZY_WEIGHTS_ORGANIZATION: dict[str, float] = {
    "npi": 0.25,
    "ein": 0.15,
    "name": 0.40,
    "address": 0.10,
    "zip": 0.05,
    "state": 0.05,
}
FUZZY_ACCEPT = 0.88
FUZZY_REJECT = 0.70


@dataclass
class FuzzyStrategy:
    """Weighted RapidFuzz similarity with hand-tuned weights and thresholds.

    The two weaknesses this baseline is built to demonstrate:

    1. **Missing and disagreeing are the same event.** Both contribute zero to
       the weighted mean, so a record with no date of birth is penalized exactly
       as hard as one whose date of birth belongs to somebody else. Most
       exclusion records are missing most fields, so this is not an edge case.
    2. **The weights never learn.** `name: 0.30` is a guess. It cannot discover
       that agreeing on a rare surname is worth more than agreeing on a common
       one, because it has no notion of how common anything is.
    """

    name: StrategyName = StrategyName.FUZZY
    accept: float = FUZZY_ACCEPT
    reject: float = FUZZY_REJECT

    def decide(self, record: NormalizedRecord, candidates: Sequence[CandidatePair]) -> MatchResult:
        kind = _kind_of(record)
        if not candidates:
            return _empty(record, kind)

        scored: list[ScoredCandidate] = []
        for provider, candidate in candidates:
            similarity, parts = self._similarity(record, provider, kind)
            scored.append(
                ScoredCandidate(
                    provider_id=provider.record_id,
                    rank=0,
                    kind=kind,
                    vector=(),
                    levels={k: f"{v:.3f}" for k, v in parts.items()},
                    match_weight=0.0,
                    posterior=similarity,
                    confidence=similarity,
                    field_weights=(),
                    blocking_keys=tuple(candidate.blocking_keys),
                )
            )
        scored.sort(key=lambda c: (-c.confidence, c.provider_id))
        ranked = tuple(replace(c, rank=i + 1) for i, c in enumerate(scored[:5]))

        top = ranked[0]
        if top.confidence >= self.accept:
            decision, reason = Outcome.MATCH, DecisionReason.ABOVE_ACCEPT
        elif top.confidence < self.reject:
            decision, reason = Outcome.NO_MATCH, DecisionReason.BELOW_REJECT
        else:
            decision, reason = Outcome.AMBIGUOUS, DecisionReason.GREY_BAND
        return MatchResult(
            record_id=record.record_id,
            decision=decision,
            route=Route.PROBABILISTIC,
            reason=reason,
            kind=kind,
            chosen_provider_id=top.provider_id if decision is not Outcome.NO_MATCH else None,
            confidence=top.confidence,
            margin=top.confidence - ranked[1].confidence if len(ranked) > 1 else None,
            candidates=ranked,
        )

    @staticmethod
    def _similarity(
        record: NormalizedRecord, provider: NormalizedRecord, kind: ModelKind
    ) -> tuple[float, dict[str, float]]:
        parts: dict[str, float] = {}
        if kind is ModelKind.ORGANIZATION:
            weights = FUZZY_WEIGHTS_ORGANIZATION
            parts["name"] = max(
                token_set(record.org_name_norm, provider.org_name_norm),
                token_set(record.dba_name_norm, provider.org_name_norm),
                token_set(record.org_name_norm, provider.dba_name_norm),
            )
            parts["ein"] = 1.0 if record.ein and record.ein == provider.ein else 0.0
        else:
            weights = FUZZY_WEIGHTS_INDIVIDUAL
            parts["name"] = max(
                jaro_winkler(record.name_norm, provider.name_norm),
                token_set(record.name_sorted_norm, provider.name_sorted_norm),
            )
            parts["dob"] = (
                1.0 if record.dob and provider.dob and record.dob == provider.dob else 0.0
            )
            parts["license"] = (
                1.0
                if record.license_number and record.license_number == provider.license_number
                else 0.0
            )
        parts["npi"] = (
            1.0 if record.npi_status is NpiStatus.VALID and record.npi == provider.npi else 0.0
        )
        parts["address"] = token_set(record.address.line, provider.address.line)
        parts["zip"] = (
            1.0 if record.address.zip5 and record.address.zip5 == provider.address.zip5 else 0.0
        )
        parts["state"] = (
            1.0 if record.address.state and record.address.state == provider.address.state else 0.0
        )
        total = sum(weights[k] * v for k, v in parts.items() if k in weights)
        return total, parts

    def stats(self) -> dict[str, Any]:
        return {"strategy": str(self.name), "accept": self.accept, "reject": self.reject}


# --------------------------------------------------------------------------
# probabilistic
# --------------------------------------------------------------------------


@dataclass
class ProbabilisticStrategy:
    """The Fellegi-Sunter engine, unassisted."""

    engine: MatchingEngine
    name: StrategyName = StrategyName.PROBABILISTIC

    def decide(self, record: NormalizedRecord, candidates: Sequence[CandidatePair]) -> MatchResult:
        return self.engine.score_record(record, candidates)

    def stats(self) -> dict[str, Any]:
        return {"strategy": str(self.name)}


@dataclass
class ProbabilisticLlmStrategy:
    """The engine, with the grey band routed to an adjudicator.

    The adjudicator is whatever satisfies the `Adjudicator` protocol; before
    Stage 4 that is `NullAdjudicator`, which abstains. Abstention leaves the
    record `AMBIGUOUS`, which is the same answer the unassisted engine gave, so
    the cell is comparable and the reported call count says plainly that no
    model was consulted.
    """

    engine: MatchingEngine
    adjudicator: Any = None
    name: StrategyName = StrategyName.PROBABILISTIC_LLM

    def __post_init__(self) -> None:
        if self.adjudicator is None:
            self.adjudicator = NullAdjudicator()

    def decide(self, record: NormalizedRecord, candidates: Sequence[CandidatePair]) -> MatchResult:
        result = self.engine.score_record(record, candidates)
        if result.decision is not Outcome.AMBIGUOUS or not result.candidates:
            return result
        request = adjudication_request(self.engine, result)
        return apply_adjudication(result, request, self.adjudicator.adjudicate(request))

    def stats(self) -> dict[str, Any]:
        return {"strategy": str(self.name), **self.adjudicator.stats()}


def adjudication_request(
    engine: MatchingEngine, result: MatchResult, top_k: int = 3
) -> AdjudicationRequest:
    """What an adjudicator is shown for this result: its candidates and the band."""
    thresholds = engine.bundle(result.kind).thresholds
    return AdjudicationRequest.of(
        result, (thresholds.t_auto_reject, thresholds.t_auto_accept), top_k=top_k
    )


def apply_adjudication(
    result: MatchResult, request: AdjudicationRequest, outcome: Any
) -> MatchResult:
    """The engine's result, with an adjudicator's answer applied to it.

    Shared by the routed strategy and the Lab's LLM-on-everything baseline, so
    the two differ only in which records they send and never in how an answer
    is read. An abstention leaves the result exactly as the engine left it.
    """
    if outcome.abstained:
        return result

    cited = set(outcome.evidence_cited)
    allowed = request.supplied_evidence()
    if not cited <= allowed:
        # The adjudicator cited a field it was never given. Rejecting the
        # response outright is cheaper and more reliable than trying to
        # decide which half of it to believe.
        return MatchResult(
            record_id=result.record_id,
            decision=Outcome.AMBIGUOUS,
            route=Route.LLM,
            reason=DecisionReason.GREY_BAND,
            kind=result.kind,
            chosen_provider_id=result.chosen_provider_id,
            confidence=result.confidence,
            match_weight=result.match_weight,
            margin=result.margin,
            candidates=result.candidates,
            notes=(
                *result.notes,
                "adjudicator response rejected: cited evidence not supplied "
                + str(sorted(cited - allowed)),
            ),
        )
    return MatchResult(
        record_id=result.record_id,
        decision=outcome.decision,
        route=Route.LLM,
        reason=DecisionReason.ADJUDICATED,
        kind=result.kind,
        chosen_provider_id=outcome.provider_id,
        confidence=outcome.confidence if outcome.confidence is not None else result.confidence,
        match_weight=result.match_weight,
        margin=result.margin,
        candidates=result.candidates,
        notes=(*result.notes, outcome.reasoning) if outcome.reasoning else result.notes,
    )


def build_strategy(
    name: StrategyName | str,
    engine: MatchingEngine | None = None,
    adjudicator: Any = None,
) -> Strategy:
    """One strategy by name. The two baselines need no fitted engine."""
    chosen = StrategyName(name)
    if chosen is StrategyName.DETERMINISTIC:
        return DeterministicStrategy()
    if chosen is StrategyName.FUZZY:
        return FuzzyStrategy()
    if engine is None:
        raise ValueError(f"strategy {chosen} needs a fitted engine")
    if chosen is StrategyName.PROBABILISTIC:
        return ProbabilisticStrategy(engine)
    return ProbabilisticLlmStrategy(engine, adjudicator)


__all__ = [
    "ALL_STRATEGIES",
    "DeterministicStrategy",
    "FuzzyStrategy",
    "ProbabilisticLlmStrategy",
    "ProbabilisticStrategy",
    "Strategy",
    "StrategyName",
    "adjudication_request",
    "apply_adjudication",
    "build_strategy",
]
