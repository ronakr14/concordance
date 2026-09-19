"""Every enumerated value in the schema, defined once.

These are the vocabulary the database enforces with `CHECK` constraints and the
vocabulary the API validates against - the same list, not two lists that drift.
`check_values()` builds the constraint from the enum, so adding a member and
generating a migration is the whole change.

Several of these mirror an enum that already exists in the engine
(`domain.Outcome`, `matching.scorer.Route`, `matching.strategies.StrategyName`).
They are restated rather than imported because `db/` is allowed to depend on
`matching/` but the reverse is forbidden by the Stage 0 seam, and a column
constraint is a storage concern. The tests assert the two agree, so a divergence
fails loudly instead of silently accepting a value the engine can never produce.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from sqlalchemy import CheckConstraint


def values(enum: type[StrEnum]) -> tuple[str, ...]:
    return tuple(str(member) for member in enum)


def check_values(column: str, enum: type[StrEnum], name: str | None = None) -> CheckConstraint:
    """A `CHECK (column IN (...))` built from the enum's members.

    The constraint is named explicitly - see `db.base` on why every constraint
    in this schema carries a name.
    """
    allowed = ", ".join(f"'{v}'" for v in values(enum))
    return CheckConstraint(f"{column} IN ({allowed})", name=name or f"{column}_valid")


class UserRole(StrEnum):
    ANALYST = "analyst"
    ADMIN = "admin"


class SanctionFileStatus(StrEnum):
    """Two-phase upload (PLAN Q1).

    A file is `INSPECTED` when it has been parsed and its columns profiled but
    nothing has been committed; `COMMITTED` once its rows are real
    `sanction_records`; `REJECTED` when the uploader abandoned it at the mapping
    step. The point of the intermediate state is that a mis-mapped file never
    becomes matchable data.
    """

    INSPECTED = "INSPECTED"
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FittedFrom(StrEnum):
    EM = "em"
    SUPERVISED = "supervised"
    #: EM over a run's candidate pairs with reviewer-labelled pairs clamped to
    #: their label. What `concordance retune` writes.
    SEMI_SUPERVISED = "semi_supervised"
    MANUAL = "manual"


class Decision(StrEnum):
    """Mirrors `domain.Outcome`."""

    MATCH = "MATCH"
    AMBIGUOUS = "AMBIGUOUS"
    NO_MATCH = "NO_MATCH"


class Route(StrEnum):
    """Mirrors the decisive routes of `matching.scorer.Route`.

    The engine also has a `no_candidates` route, which is a reason rather than a
    decision path and is recorded in `match_results.explanation`.
    """

    DETERMINISTIC = "deterministic"
    PROBABILISTIC = "probabilistic"
    LLM = "llm"


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"


class CaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class CasePhase(StrEnum):
    """What a person reads for a case: the stored status, with `ACTIVE` split.

    Derived, never stored. A case opened with a future `start_date` is stored
    `ACTIVE` - the partial unique index and the expiry job both need it to be -
    but its window has not begun, so it reads as `PENDING`. Worked out at query
    time, as provider compliance is, so it cannot go stale overnight.
    """

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


def case_phase(status: str, start_date: date, today: date) -> CasePhase:
    if status == CaseStatus.ACTIVE and start_date > today:
        return CasePhase.PENDING
    return CasePhase(status)


class ExpectedOutcome(StrEnum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    AMBIGUOUS = "AMBIGUOUS"


class EvalStrategy(StrEnum):
    DETERMINISTIC = "deterministic"
    FUZZY = "fuzzy"
    PROBABILISTIC = "probabilistic"
    PROBABILISTIC_LLM = "probabilistic_llm"


class LabKind(StrEnum):
    """What a Lab experiment measured."""

    SWEEP = "sweep"
    LLM = "llm"
    #: Simulated review rounds: label, retune, rescore, repeat.
    FEEDBACK = "feedback"


class FeedbackLabel(StrEnum):
    TRUE_MATCH = "TRUE_MATCH"
    FALSE_MATCH = "FALSE_MATCH"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    DEAD = "DEAD"


class ProviderStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    RETIRED = "RETIRED"


__all__ = [
    "CasePhase",
    "CaseStatus",
    "Decision",
    "EvalStrategy",
    "ExpectedOutcome",
    "FeedbackLabel",
    "FittedFrom",
    "JobStatus",
    "ProviderStatus",
    "ReviewStatus",
    "Route",
    "RunStatus",
    "SanctionFileStatus",
    "UserRole",
    "case_phase",
    "check_values",
    "values",
]
