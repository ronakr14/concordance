"""Scoring one sanction record against its candidate providers.

The order of the two paths is the whole design:

**Deterministic first.** A checksum-valid NPI that agrees exactly is an
identifier match, and no probabilistic model should be allowed to talk anyone
out of it. But agreement on the identifier is *not* licence to ignore the rest
of the record: a pair whose NPIs agree and whose date of birth *and* state both
contradict each other is more likely to be a data-entry error in the identifier
column than a person who moved and was reborn. That rule comes from the source
spec's Role 2 stage 3 and it is the difference between a deterministic path and
a credulous one.

How hard the rule bites is graded, because the unguarded version is wrong in
the other direction. At corruption 0.5 roughly one record in seven with a
perfectly good NPI also has a date of birth that is off by a year, and treating
a single contradiction as disqualifying sent 120 genuine matches to a human for
no reason - measurably, the `exact_npi` scenario lost fifteen points of recall.
So:

- no contradictions - accept, deterministically;
- exactly one - decline to short-circuit and let the probabilistic path decide,
  which already knows both that the NPI agrees and that the field conflicts;
- two or more - `AMBIGUOUS`, flagged for review, and say which fields.

**Probabilistic second.** Comparison vector, Fellegi-Sunter match weight,
posterior, calibrator, and then three decisions rather than two:

- above `t_auto_accept` -> `MATCH`
- below `t_auto_reject` -> `NO_MATCH`
- between -> the grey band, which is `AMBIGUOUS` until an adjudicator says
  otherwise

with one override on top. **The margin check:** if the best and second-best
candidates are within `margin_delta` of each other, the answer is `AMBIGUOUS`
no matter how high the absolute confidence is. Two providers at 0.97 and 0.96
are not a 0.97 match; they are a coin toss between two people, and the planted
twin and common-name clusters exist to produce exactly that. Absolute
confidence alone cannot see it, because both candidates genuinely do look like
the record.

Every result carries its full evidence: the per-field agreement levels, the
per-field weight contributions, the blocking keys that surfaced each candidate,
and the route that produced the decision. The Investigation UI at Stage 8 needs
all of it, and so does anyone asking why a particular provider was flagged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from concordance.domain import Candidate, Outcome
from concordance.matching.calibration import IsotonicCalibrator, Thresholds
from concordance.matching.comparators import (
    ComparisonVector,
    ModelKind,
    as_organization,
    compare,
    describe,
    pair_kind,
)
from concordance.matching.fellegi_sunter import FellegiSunterModel
from concordance.matching.normalization import NormalizedRecord
from concordance.matching.npi_validator import NpiStatus

DEFAULT_TOP_K = 5
DEFAULT_MARGIN_DELTA = 0.05


class Route(StrEnum):
    """How a decision was reached. Recorded on every result."""

    DETERMINISTIC = "deterministic"
    PROBABILISTIC = "probabilistic"
    LLM = "llm"
    NO_CANDIDATES = "no_candidates"


class DecisionReason(StrEnum):
    """Why, in one token. The UI groups on it and the eval harness counts it."""

    NPI_EXACT = "npi_exact"
    NPI_EXACT_CONFLICT = "npi_exact_with_conflicting_attributes"
    ABOVE_ACCEPT = "above_accept_threshold"
    BELOW_REJECT = "below_reject_threshold"
    GREY_BAND = "grey_band"
    THIN_MARGIN = "thin_margin_between_top_candidates"
    NO_CANDIDATES = "no_candidates_from_blocking"
    CROSS_TYPE = "cross_type_pair_coerced_to_organization"
    ADJUDICATED = "adjudicated"


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """One provider, scored against one sanction record."""

    provider_id: str
    rank: int
    kind: ModelKind
    vector: ComparisonVector
    levels: dict[str, str]
    match_weight: float
    posterior: float
    confidence: float
    field_weights: tuple[dict[str, Any], ...]
    blocking_keys: tuple[str, ...]
    cross_type: bool = False
    npi_exact: bool = False
    conflicts: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "rank": self.rank,
            "model": str(self.kind),
            "levels": dict(self.levels),
            "match_weight": round(self.match_weight, 6),
            "posterior": round(self.posterior, 8),
            "confidence": round(self.confidence, 8),
            "field_weights": [dict(f) for f in self.field_weights],
            "blocking_keys": list(self.blocking_keys),
            "cross_type": self.cross_type,
            "npi_exact": self.npi_exact,
            "conflicts": list(self.conflicts),
        }


@dataclass(frozen=True, slots=True)
class MatchResult:
    """The engine's answer for one sanction record."""

    record_id: str
    decision: Outcome
    route: Route
    reason: DecisionReason
    kind: ModelKind
    chosen_provider_id: str | None = None
    confidence: float = 0.0
    match_weight: float = 0.0
    margin: float | None = None
    candidates: tuple[ScoredCandidate, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def top(self) -> ScoredCandidate | None:
        return self.candidates[0] if self.candidates else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "decision": str(self.decision),
            "route": str(self.route),
            "reason": str(self.reason),
            "model": str(self.kind),
            "chosen_provider_id": self.chosen_provider_id,
            "confidence": round(self.confidence, 8),
            "match_weight": round(self.match_weight, 6),
            "margin": None if self.margin is None else round(self.margin, 8),
            "candidates": [c.as_dict() for c in self.candidates],
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# the fitted configuration
# --------------------------------------------------------------------------


@dataclass
class ModelBundle:
    """One model kind's fitted parameters, calibrator and thresholds.

    Individuals and organizations each get one of these, and neither model's
    statistics touch the other's (PLAN 11.2). Keeping the calibrator and the
    thresholds beside the m/u tables rather than in a shared block is what makes
    that separation structural instead of a convention somebody has to remember.
    """

    model: FellegiSunterModel
    calibrator: IsotonicCalibrator
    thresholds: Thresholds
    calibration: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.to_dict(),
            "calibrator": self.calibrator.as_dict(),
            "thresholds": self.thresholds.as_dict(),
            "calibration": self.calibration,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ModelBundle:
        return cls(
            model=FellegiSunterModel.from_dict(payload["model"]),
            calibrator=IsotonicCalibrator.from_dict(payload["calibrator"]),
            thresholds=Thresholds.from_dict(payload["thresholds"]),
            calibration=dict(payload.get("calibration", {})),
        )


# --------------------------------------------------------------------------
# deterministic path
# --------------------------------------------------------------------------

# Attributes that, when they contradict, make an exact NPI agreement suspicious
# enough to send to a human. Each is a field where two records about the same
# person essentially cannot differ.
CONFLICT_FIELDS = ("dob", "state", "last_name")

# One contradiction is ordinary corruption; two is a reason to stop. See the
# module docstring for the measurement behind the number.
MIN_CONFLICTS_FOR_REVIEW = 2


def deterministic_conflicts(levels: Mapping[str, str]) -> tuple[str, ...]:
    """Fields that actively contradict, ignoring the ones that are merely absent.

    `MISSING` is not a conflict. That is the point of it being its own level:
    an exclusion record with no date of birth is the normal case, and treating
    it as evidence against an identifier match would reject most of the file.
    """
    return tuple(f for f in CONFLICT_FIELDS if levels.get(f) == "DISAGREE")


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------


@dataclass
class MatchingEngine:
    """Scores records. Holds no storage - candidates and providers are handed in.

    Storage-free on purpose: the same engine object serves the Parquet store
    now, the Postgres store at Stage 5 and the API at Stage 7, and the sweep can
    run it ten times in parallel without a database anywhere.
    """

    individual: ModelBundle
    organization: ModelBundle
    top_k: int = DEFAULT_TOP_K
    margin_delta: float = DEFAULT_MARGIN_DELTA

    def bundle(self, kind: ModelKind) -> ModelBundle:
        return self.individual if kind is ModelKind.INDIVIDUAL else self.organization

    # -- one pair ---------------------------------------------------------
    def score_pair(
        self,
        record: NormalizedRecord,
        provider: NormalizedRecord,
        blocking_keys: Sequence[str] = (),
    ) -> ScoredCandidate:
        """Compare one pair and score it under the model the pair belongs to."""
        kind = pair_kind(record, provider)
        cross_type = kind is None
        if cross_type:
            # Explicitly coerced, never silently scored: an organization filed
            # in the person columns is a scenario in its own right, and
            # discarding the pair would make that scenario unmatchable.
            record = as_organization(record)
            provider = as_organization(provider)
            kind = ModelKind.ORGANIZATION

        assert kind is not None
        bundle = self.bundle(kind)
        vector = compare(record, provider, kind)
        score = bundle.model.score(vector)
        levels = describe(vector, kind)
        # The calibrator takes the *match weight*, not the posterior: the
        # posterior saturates at 1.0 and loses the ordering the calibration
        # needs. See `calibration.py`.
        confidence = bundle.calibrator.apply(score.match_weight)
        npi_exact = (
            record.npi_status is NpiStatus.VALID
            and provider.npi_status is NpiStatus.VALID
            and record.npi == provider.npi
        )
        return ScoredCandidate(
            provider_id=provider.record_id,
            rank=0,
            kind=kind,
            vector=vector,
            levels=levels,
            match_weight=score.match_weight,
            posterior=score.posterior,
            confidence=confidence,
            field_weights=tuple(c.as_dict() for c in score.contributions),
            blocking_keys=tuple(blocking_keys),
            cross_type=cross_type,
            npi_exact=npi_exact,
            conflicts=deterministic_conflicts(levels) if npi_exact else (),
        )

    # -- one record -------------------------------------------------------
    def score_record(
        self,
        record: NormalizedRecord,
        candidates: Sequence[tuple[NormalizedRecord, Candidate]],
    ) -> MatchResult:
        """Score every candidate, rank them, and route the top one."""
        default_kind = ModelKind.ORGANIZATION if record.is_organization else ModelKind.INDIVIDUAL
        if not candidates:
            return MatchResult(
                record_id=record.record_id,
                decision=Outcome.NO_MATCH,
                route=Route.NO_CANDIDATES,
                reason=DecisionReason.NO_CANDIDATES,
                kind=default_kind,
            )

        scored = [
            self.score_pair(record, provider, candidate.blocking_keys)
            for provider, candidate in candidates
        ]
        # Confidence descending, then provider id, so ties resolve the same way
        # on every run and a replay reproduces the ranking exactly.
        scored.sort(key=lambda c: (-c.confidence, -c.match_weight, c.provider_id))
        ranked = tuple(replace(c, rank=i + 1) for i, c in enumerate(scored[: self.top_k]))
        return self._route(record, ranked)

    def _route(
        self,
        record: NormalizedRecord,
        ranked: tuple[ScoredCandidate, ...],
    ) -> MatchResult:
        top = ranked[0]
        kind = top.kind
        bundle = self.bundle(kind)
        thresholds = bundle.thresholds
        notes: list[str] = []
        if top.cross_type:
            notes.append(
                "source and master disagree on organization/individual; "
                "scored under the organization model"
            )

        margin = top.confidence - ranked[1].confidence if len(ranked) > 1 else None

        # -- deterministic path -------------------------------------------
        npi_exact = [c for c in ranked if c.npi_exact]
        if npi_exact:
            best = npi_exact[0]
            if len(best.conflicts) >= MIN_CONFLICTS_FOR_REVIEW:
                return MatchResult(
                    record_id=record.record_id,
                    decision=Outcome.AMBIGUOUS,
                    route=Route.DETERMINISTIC,
                    reason=DecisionReason.NPI_EXACT_CONFLICT,
                    kind=kind,
                    chosen_provider_id=best.provider_id,
                    confidence=best.confidence,
                    match_weight=best.match_weight,
                    margin=margin,
                    candidates=ranked,
                    notes=(
                        *notes,
                        "NPI agrees exactly but "
                        + ", ".join(best.conflicts)
                        + " contradict; sent to review rather than accepted",
                    ),
                )
            if len(npi_exact) > 1:
                # Two providers holding the same valid NPI is a master-data
                # defect, not a match. Saying so is more useful than picking one.
                return MatchResult(
                    record_id=record.record_id,
                    decision=Outcome.AMBIGUOUS,
                    route=Route.DETERMINISTIC,
                    reason=DecisionReason.NPI_EXACT_CONFLICT,
                    kind=kind,
                    chosen_provider_id=best.provider_id,
                    confidence=best.confidence,
                    match_weight=best.match_weight,
                    margin=margin,
                    candidates=ranked,
                    notes=(*notes, "more than one provider carries this NPI in the master"),
                )
            if not best.conflicts:
                return MatchResult(
                    record_id=record.record_id,
                    decision=Outcome.MATCH,
                    route=Route.DETERMINISTIC,
                    reason=DecisionReason.NPI_EXACT,
                    kind=kind,
                    chosen_provider_id=best.provider_id,
                    confidence=max(best.confidence, thresholds.t_auto_accept),
                    match_weight=best.match_weight,
                    margin=margin,
                    candidates=ranked,
                    notes=tuple(notes),
                )
            notes.append(
                "NPI agrees exactly but "
                + ", ".join(best.conflicts)
                + " contradicts; decided on the probabilistic path rather than "
                "short-circuited"
            )

        # -- probabilistic path -------------------------------------------
        reason = DecisionReason.CROSS_TYPE if top.cross_type else DecisionReason.ABOVE_ACCEPT
        if top.confidence >= thresholds.t_auto_accept:
            if margin is not None and margin < self.margin_delta:
                # Absolute confidence cannot see a tie. Two providers that both
                # look right are a decision for a human, not a coin toss.
                return MatchResult(
                    record_id=record.record_id,
                    decision=Outcome.AMBIGUOUS,
                    route=Route.PROBABILISTIC,
                    reason=DecisionReason.THIN_MARGIN,
                    kind=kind,
                    chosen_provider_id=top.provider_id,
                    confidence=top.confidence,
                    match_weight=top.match_weight,
                    margin=margin,
                    candidates=ranked,
                    notes=(
                        *notes,
                        f"top two candidates within {margin:.4f} < {self.margin_delta}",
                    ),
                )
            return MatchResult(
                record_id=record.record_id,
                decision=Outcome.MATCH,
                route=Route.PROBABILISTIC,
                reason=reason,
                kind=kind,
                chosen_provider_id=top.provider_id,
                confidence=top.confidence,
                match_weight=top.match_weight,
                margin=margin,
                candidates=ranked,
                notes=tuple(notes),
            )

        if top.confidence < thresholds.t_auto_reject:
            return MatchResult(
                record_id=record.record_id,
                decision=Outcome.NO_MATCH,
                route=Route.PROBABILISTIC,
                reason=DecisionReason.BELOW_REJECT,
                kind=kind,
                chosen_provider_id=None,
                confidence=top.confidence,
                match_weight=top.match_weight,
                margin=margin,
                candidates=ranked,
                notes=tuple(notes),
            )

        return MatchResult(
            record_id=record.record_id,
            decision=Outcome.AMBIGUOUS,
            route=Route.PROBABILISTIC,
            reason=DecisionReason.GREY_BAND,
            kind=kind,
            chosen_provider_id=top.provider_id,
            confidence=top.confidence,
            match_weight=top.match_weight,
            margin=margin,
            candidates=ranked,
            notes=tuple(notes),
        )

    # -- serialization ----------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "individual": self.individual.as_dict(),
            "organization": self.organization.as_dict(),
            "top_k": self.top_k,
            "margin_delta": self.margin_delta,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MatchingEngine:
        return cls(
            individual=ModelBundle.from_dict(payload["individual"]),
            organization=ModelBundle.from_dict(payload["organization"]),
            top_k=int(payload.get("top_k", DEFAULT_TOP_K)),
            margin_delta=float(payload.get("margin_delta", DEFAULT_MARGIN_DELTA)),
        )
