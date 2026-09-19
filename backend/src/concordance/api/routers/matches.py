"""`/matches` - the review queue, the Investigation detail, and the verdicts.

Roles: any user can list, read, reject and escalate; only an admin can approve,
because approval opens a case and a case is a compliance action. Rejecting an
escalated match is also admin-only - escalating is how an analyst hands a
decision up, and handing it back down by rejecting it would defeat that.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from concordance.api import presenters, schemas
from concordance.api.deps import ActorDep, AdminActor, CurrentUser, SessionDep, SettingsDep
from concordance.api.routers.common import Limit, Offset
from concordance.db.repositories.matches import MatchFilter, MatchRepository
from concordance.review import service as review

router = APIRouter(prefix="/matches", tags=["matches"])

DecisionQ = Literal["MATCH", "AMBIGUOUS", "NO_MATCH"]
ReviewQ = Literal["PENDING", "APPROVED", "REJECTED", "ESCALATED"]
RecordTypeQ = Literal["individual", "organization"]


@router.get("", response_model=schemas.Page[schemas.MatchListItemOut])
def list_matches(
    session: SessionDep,
    _user: CurrentUser,
    limit: Limit = 50,
    offset: Offset = 0,
    run_id: uuid.UUID | None = None,
    decision: DecisionQ | None = None,
    review_status: ReviewQ | None = None,
    min_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    max_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    state: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    sanction_type: Annotated[str | None, Query(max_length=100)] = None,
    record_type: RecordTypeQ | None = None,
    conflict: Annotated[
        bool | None, Query(description="Only results involved in a conflict on a live case.")
    ] = None,
    audit: Annotated[
        bool | None, Query(description="Only results drawn into the random audit of auto-rejects.")
    ] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    include_superseded: Annotated[
        bool, Query(description="Include results a later run replaced (Q5).")
    ] = False,
    sort: Literal["confidence", "date"] = "confidence",
    order: Literal["asc", "desc"] = "desc",
) -> schemas.Page[schemas.MatchListItemOut]:
    """Current results, filtered and sorted, each beside the record it decides."""
    where = MatchFilter(
        run_id=run_id,
        decision=decision,
        review_status=review_status,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        state=state,
        sanction_type=sanction_type,
        is_organization=None if record_type is None else record_type == "organization",
        conflict=conflict,
        audit=audit,
        date_from=date_from,
        date_to=date_to,
        include_superseded=include_superseded,
        sort=sort,
        descending=order == "desc",
    )
    page = MatchRepository(session).search(where, limit=limit, offset=offset)
    return schemas.Page[schemas.MatchListItemOut](
        items=[presenters.match_list_item(result, record) for result, record in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{match_id}", response_model=schemas.MatchDetailOut)
def get_match(match_id: uuid.UUID, session: SessionDep, actor: ActorDep) -> schemas.MatchDetailOut:
    """Everything behind one decision: candidates, per-field evidence, the AI's reasoning.

    Recorded in the audit log as a view.
    """
    result = review.view(session, actor, match_id)
    detail = presenters.match_detail(session, result)
    session.commit()
    return detail


@router.post(
    "/{match_id}/approve",
    response_model=schemas.ApprovalOut,
    responses={409: {"description": "Already approved or rejected, or superseded."},
               422: {"description": "An ambiguous match needs `provider_id`, from its candidates."}},
)
def approve(
    match_id: uuid.UUID,
    body: schemas.ReviewIn,
    session: SessionDep,
    settings: SettingsDep,
    actor: AdminActor,
) -> schemas.ApprovalOut:
    """Confirm the match and open its case. Admin only."""
    approval = review.approve(
        session,
        settings,
        actor,
        match_id,
        provider_id=body.provider_id,
        comment=body.comment,
        duration_months=body.duration_months,
        start=body.start_date,
    )
    session.commit()
    return schemas.ApprovalOut(
        match=schemas.MatchOut.model_validate(approval.result),
        case=schemas.CaseOut.model_validate(approval.case),
        case_created=approval.case_created,
    )


@router.post("/{match_id}/reject", response_model=schemas.MatchOut)
def reject(
    match_id: uuid.UUID, body: schemas.CommentIn, session: SessionDep, actor: ActorDep
) -> schemas.MatchOut:
    """The proposed provider is not the sanctioned party. A comment is required."""
    result = review.reject(
        session, actor, match_id, comment=body.comment, provider_id=body.provider_id
    )
    session.commit()
    return schemas.MatchOut.model_validate(result)


@router.post("/{match_id}/escalate", response_model=schemas.MatchOut)
def escalate(
    match_id: uuid.UUID, body: schemas.CommentIn, session: SessionDep, actor: ActorDep
) -> schemas.MatchOut:
    """Hand the decision to an admin, saying why."""
    result = review.escalate(session, actor, match_id, comment=body.comment)
    session.commit()
    return schemas.MatchOut.model_validate(result)


@router.post("/bulk", response_model=schemas.BulkOut)
def bulk(body: schemas.BulkIn, session: SessionDep, actor: ActorDep) -> schemas.BulkOut:
    """Reject or escalate up to 100 results with one comment.

    Each item is decided and audited exactly as the single endpoint would, in
    its own savepoint: a refused item is reported beside its id and the rest
    still commit. The response is 200 even when some items fail - read
    `failed`. Approval is not offered in bulk.
    """
    outcomes = review.bulk(session, actor, body.ids, action=body.action, comment=body.comment)
    session.commit()
    results = [
        schemas.BulkItemOut(
            id=o.result_id,
            ok=o.ok,
            review_status=o.result.review_status if o.result is not None else None,
            error=schemas.ErrorBodyOut(
                code=o.error.code, message=o.error.message, details=o.error.details or None
            )
            if o.error is not None
            else None,
        )
        for o in outcomes
    ]
    succeeded = sum(1 for r in results if r.ok)
    return schemas.BulkOut(succeeded=succeeded, failed=len(results) - succeeded, results=results)


__all__ = ["router"]
