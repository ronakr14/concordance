"""The review workflow: a human deciding what the engine proposed.

Four rules shape this module, and each is a place where the obvious code is
wrong.

**Approval is idempotent by refusal.** The result row is locked, and a result
that is already approved is refused with a 409 naming the case it opened. The
lock is what makes it true under concurrency: two admins clicking at once
serialise on the row, and the second sees `APPROVED`. A duplicate approval that
succeeded quietly would be harmless today and a second case tomorrow.

**An ambiguous result needs an explicit choice.** The engine found two or more
plausible providers and would not commit to one - it records its top-ranked
candidate, but by a margin too small to decide on - so approving it without
naming one would be approving a coin toss. The chosen provider must be one of
the result's own candidates - an approval is a judgement about the evidence
shown, not a way to attach an arbitrary provider to a sanction.

**The engine's answer is never overwritten.** The reviewer's provider goes in
`approved_provider_id`, beside `chosen_provider_id`. When they differ, that is
the most useful row in the feedback table, and it only exists if both survive.

**Every verdict becomes training data.** Approve and reject each write a
`feedback_events` row carrying the comparison vector the reviewer saw - field
levels and weights, copied rather than referenced - so a later supervised refit
learns from exactly what was judged.

Superseded results are refused throughout: a reviewer acting on a decision a
later run has replaced is acting on history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from concordance.audit import service as audit
from concordance.audit.service import Actor
from concordance.cases.lifecycle import open_case
from concordance.config import Settings
from concordance.db.enums import Decision, FeedbackLabel, ReviewStatus
from concordance.db.models import Case, MatchCandidate, MatchResult
from concordance.db.repositories.evaluation import EvalRepository
from concordance.db.repositories.matches import MatchRepository
from concordance.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
)

#: Candidates listed in a "choose one" error, so the client can offer them.
CHOICES_LISTED = 5


@dataclass
class Approval:
    result: MatchResult
    case: Case
    case_created: bool


def _locked(session: Session, result_id: uuid.UUID) -> MatchResult:
    result = session.get(MatchResult, result_id, with_for_update=True)
    if result is None:
        raise NotFoundError(f"no match {result_id}")
    if result.superseded_by is not None:
        raise ConflictError(
            "a later run has replaced this decision; review the current one",
            code="superseded",
            details={"superseded_by": str(result.superseded_by)},
        )
    return result


def _state(result: MatchResult) -> dict[str, Any]:
    return {
        "review_status": result.review_status,
        "decision": result.decision,
        "chosen_provider_id": result.chosen_provider_id,
        "approved_provider_id": result.approved_provider_id,
    }


def _stamp(result: MatchResult, actor: Actor, status: ReviewStatus, comment: str | None) -> None:
    result.review_status = str(status)
    result.reviewed_by = actor.user_id
    result.reviewed_at = datetime.now(UTC)
    result.reviewer_comment = comment


def view(session: Session, actor: Actor, result_id: uuid.UUID) -> MatchResult:
    """Read one result for investigation, and record that it was read.

    Viewing is audited because "who looked at this before it was approved" is a
    question compliance asks, and a read leaves no trace anywhere else.
    """
    result = session.get(MatchResult, result_id)
    if result is None:
        raise NotFoundError(f"no match {result_id}")
    audit.record(
        session, actor, "match.viewed", entity_type="match_result", entity_id=result.id
    )
    return result


def approve(
    session: Session,
    settings: Settings,
    actor: Actor,
    result_id: uuid.UUID,
    *,
    provider_id: str | None = None,
    comment: str | None = None,
    duration_months: int | None = None,
    start: date | None = None,
) -> Approval:
    """Confirm a match and open its case, in one transaction."""
    result = _locked(session, result_id)
    if result.review_status == ReviewStatus.APPROVED:
        existing = session.scalar(
            select(Case).where(Case.match_result_id == result.id).order_by(Case.created_at.desc())
        )
        raise ConflictError(
            "this match has already been approved",
            code="already_approved",
            details={
                "case_id": str(existing.id) if existing else None,
                "case_number": existing.case_number if existing else None,
            },
        )
    if result.review_status == ReviewStatus.REJECTED:
        raise ConflictError(
            "this match was rejected; a rejected decision is final for this run",
            code="already_reviewed",
            details={"review_status": result.review_status},
        )

    candidates = MatchRepository(session).candidates_for(result.id)
    chosen = _choose(result, candidates, provider_id)
    before = _state(result)
    _stamp(result, actor, ReviewStatus.APPROVED, _clean(comment))
    result.approved_provider_id = chosen.provider_id

    EvalRepository(session).add_feedback(
        match_result_id=result.id,
        reviewer_id=actor.user_id,
        label=str(FeedbackLabel.TRUE_MATCH),
        comparison_vector=_vector(chosen, result),
    )

    months = duration_months or settings.DEFAULT_CASE_MONTHS
    had_case = _active_case_id(session, chosen.provider_id, result.sanction_record_id)
    case = open_case(
        session,
        provider_id=chosen.provider_id,
        sanction_record_id=result.sanction_record_id,
        match_result_id=result.id,
        duration_months=months,
        start=start,
        actor=actor,
    )
    audit.record(
        session,
        actor,
        "match.approved",
        entity_type="match_result",
        entity_id=result.id,
        before=before,
        after={
            **_state(result),
            "comment": result.reviewer_comment,
            "case_id": case.id,
            "case_number": case.case_number,
            "case_reused": had_case is not None,
        },
    )
    return Approval(result=result, case=case, case_created=had_case is None)


def reject(
    session: Session,
    actor: Actor,
    result_id: uuid.UUID,
    *,
    comment: str | None,
    provider_id: str | None = None,
) -> MatchResult:
    """Declare that the proposed provider is not the sanctioned party."""
    text = _require_comment(comment)
    result = _locked(session, result_id)
    if result.review_status in (ReviewStatus.APPROVED, ReviewStatus.REJECTED):
        raise ConflictError(
            f"this match is already {result.review_status}",
            code="already_reviewed",
            details={"review_status": result.review_status},
        )
    if result.review_status == ReviewStatus.ESCALATED and not actor.is_admin:
        raise ForbiddenError(
            "this match was escalated; an admin decides it now",
            code="escalated_to_admin",
            details={"required": ["admin"], "actual": actor.role},
        )

    before = _state(result)
    _stamp(result, actor, ReviewStatus.REJECTED, text)

    # The label attaches to the provider that was proposed: the one named, or
    # the engine's choice, or - for a result with no choice - the top
    # candidate, which is the one a reviewer is looking at when they say no.
    candidates = MatchRepository(session).candidates_for(result.id)
    target = provider_id or result.chosen_provider_id or (candidates[0].provider_id if candidates else None)
    candidate = next((c for c in candidates if c.provider_id == target), None)
    if candidate is not None:
        EvalRepository(session).add_feedback(
            match_result_id=result.id,
            reviewer_id=actor.user_id,
            label=str(FeedbackLabel.FALSE_MATCH),
            comparison_vector=_vector(candidate, result),
        )
    audit.record(
        session,
        actor,
        "match.rejected",
        entity_type="match_result",
        entity_id=result.id,
        before=before,
        after={**_state(result), "comment": text, "rejected_provider_id": target},
    )
    return result


def escalate(
    session: Session, actor: Actor, result_id: uuid.UUID, *, comment: str | None
) -> MatchResult:
    """Hand the decision to an admin, with a note saying why."""
    text = _require_comment(comment)
    result = _locked(session, result_id)
    if result.review_status != ReviewStatus.PENDING:
        raise ConflictError(
            f"only a pending match can be escalated; this one is {result.review_status}",
            code="not_pending",
            details={"review_status": result.review_status},
        )
    before = _state(result)
    _stamp(result, actor, ReviewStatus.ESCALATED, text)
    audit.record(
        session,
        actor,
        "match.escalated",
        entity_type="match_result",
        entity_id=result.id,
        before=before,
        after={**_state(result), "comment": text},
    )
    return result


@dataclass
class BulkOutcome:
    result_id: uuid.UUID
    result: MatchResult | None = None
    error: DomainError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def bulk(
    session: Session,
    actor: Actor,
    result_ids: list[uuid.UUID],
    *,
    action: str,
    comment: str | None,
) -> list[BulkOutcome]:
    """Reject or escalate many results, each on its own terms.

    Each item runs in a savepoint, so one refusal - already reviewed, escalated
    past an analyst, superseded - rolls back that item alone and is reported
    beside it, while the rest commit together. Every item is decided by the
    same rule as the single endpoint and audited as its own action; a batch is
    a convenience for the reviewer, not a second, looser path.

    Rows are locked in id order rather than request order, so two overlapping
    batches acquire their locks in the same sequence and cannot deadlock.

    Approval is not offered: it opens a case, and a compliance action taken on
    a hundred records at once is a hundred actions nobody looked at.
    """
    if action not in ("reject", "escalate"):
        raise InvalidError(f"bulk {action} is not supported", details={"action": "reject or escalate"})
    text = _require_comment(comment)
    unique = list(dict.fromkeys(result_ids))
    outcomes: dict[uuid.UUID, BulkOutcome] = {}
    for result_id in sorted(unique):
        try:
            with session.begin_nested():
                if action == "reject":
                    result = reject(session, actor, result_id, comment=text)
                else:
                    result = escalate(session, actor, result_id, comment=text)
            outcomes[result_id] = BulkOutcome(result_id, result=result)
        except DomainError as exc:
            outcomes[result_id] = BulkOutcome(result_id, error=exc)

    # One row for the batch itself, beside each item's own. The item rows say
    # what changed; this one also records what was attempted and refused,
    # which no item row can - a refused item changed nothing.
    audit.record(
        session,
        actor,
        "match.bulk_reviewed",
        entity_type="review_batch",
        entity_id=actor.request_id or uuid.uuid4().hex,
        after={
            "action": action,
            "comment": text,
            "succeeded": [str(i) for i in unique if outcomes[i].ok],
            "refused": {
                str(i): outcomes[i].error.code  # type: ignore[union-attr]
                for i in unique
                if not outcomes[i].ok
            },
        },
    )
    return [outcomes[result_id] for result_id in unique]


def create_case(
    session: Session,
    settings: Settings,
    actor: Actor,
    match_result_id: uuid.UUID,
    *,
    duration_months: int | None = None,
    start: date | None = None,
) -> Case:
    """Open a case on an approved match that has no live one.

    Approval opens a case already, so this exists for the case that approval's
    case has ended: closed early or expired, and the provider still needs
    watching. A second active case for the same provider and record is refused.
    """
    result = session.get(MatchResult, match_result_id)
    if result is None:
        raise NotFoundError(f"no match {match_result_id}")
    if result.review_status != ReviewStatus.APPROVED or not result.approved_provider_id:
        raise ConflictError(
            "a case is opened on an approved match; approve this one first",
            code="not_approved",
            details={"review_status": result.review_status},
        )
    return open_case(
        session,
        provider_id=result.approved_provider_id,
        sanction_record_id=result.sanction_record_id,
        match_result_id=result.id,
        duration_months=duration_months or settings.DEFAULT_CASE_MONTHS,
        start=start,
        actor=actor,
        strict=True,
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _choose(
    result: MatchResult, candidates: list[MatchCandidate], provider_id: str | None
) -> MatchCandidate:
    options = [c.provider_id for c in candidates[:CHOICES_LISTED]]
    if provider_id is None:
        if result.decision == Decision.AMBIGUOUS:
            raise InvalidError(
                "this match is ambiguous; name the provider you are approving",
                code="provider_choice_required",
                details={"provider_id": "required for an ambiguous match", "candidates": options},
            )
        provider_id = result.chosen_provider_id
    if provider_id is None:
        raise InvalidError(
            "the engine chose no provider for this record; name one of its candidates",
            code="provider_required",
            details={"provider_id": "required", "candidates": options},
        )
    for candidate in candidates:
        if candidate.provider_id == provider_id:
            return candidate
    raise InvalidError(
        f"provider {provider_id} is not a candidate for this record",
        code="provider_not_a_candidate",
        details={"provider_id": "must be one of the result's candidates", "candidates": options},
    )


def _vector(candidate: MatchCandidate, result: MatchResult) -> dict[str, Any]:
    return {
        "provider_id": candidate.provider_id,
        "rank": candidate.rank,
        "field_levels": dict(candidate.field_levels or {}),
        "field_weights": dict(candidate.field_weights or {}),
        "match_weight": candidate.match_weight,
        "posterior": candidate.posterior,
        "decision": result.decision,
        "route": result.route,
        "model": (result.explanation or {}).get("model"),
        "run_id": str(result.run_id),
    }


def _active_case_id(session: Session, provider_id: str, sanction_record_id: uuid.UUID) -> Any:
    from concordance.db.repositories.cases import CaseRepository

    found = CaseRepository(session).active_for(provider_id, sanction_record_id)
    return found.id if found else None


def _clean(comment: str | None) -> str | None:
    text = (comment or "").strip()
    return text or None


def _require_comment(comment: str | None) -> str:
    text = _clean(comment)
    if text is None:
        raise InvalidError("a comment is required", details={"comment": "say why"})
    return text


__all__ = ["Approval", "BulkOutcome", "approve", "bulk", "create_case", "escalate", "reject", "view"]
