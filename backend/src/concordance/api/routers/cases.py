"""`/cases` - open, list, read, close, and each case's own audit trail."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status

from concordance.api import presenters, schemas
from concordance.api.deps import AdminActor, CurrentUser, SessionDep, SettingsDep
from concordance.api.errors import NotFoundError
from concordance.api.routers.common import Limit, Offset, page_of
from concordance.cases.lifecycle import close_case
from concordance.db.repositories.audit import AuditRepository
from concordance.db.repositories.cases import CaseRepository
from concordance.review import service as review

router = APIRouter(prefix="/cases", tags=["cases"])

CaseStatusQ = Literal["ACTIVE", "EXPIRED", "CLOSED", "REJECTED"]


@router.post(
    "",
    response_model=schemas.CaseOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "The match is not approved, or a case is already active."}},
)
def create(
    body: schemas.CaseIn, session: SessionDep, settings: SettingsDep, actor: AdminActor
) -> schemas.CaseOut:
    """Open a case on an approved match. Approval already opens one; this is for
    re-opening after that case closed or expired. Admin only."""
    case = review.create_case(
        session,
        settings,
        actor,
        body.match_result_id,
        duration_months=body.duration_months,
        start=body.start_date,
    )
    session.commit()
    return schemas.CaseOut.model_validate(case)


@router.get("", response_model=schemas.Page[schemas.CaseOut])
def list_cases(
    session: SessionDep,
    _user: CurrentUser,
    limit: Limit = 50,
    offset: Offset = 0,
    status_: Annotated[CaseStatusQ | None, Query(alias="status")] = None,
    provider_id: Annotated[str | None, Query(max_length=64)] = None,
    conflict: bool = False,
    date_from: Annotated[date | None, Query(description="Start date on or after.")] = None,
    date_to: Annotated[date | None, Query(description="Start date on or before.")] = None,
) -> schemas.Page[schemas.CaseOut]:
    page = CaseRepository(session).list_cases(
        status=status_,
        conflicts_only=conflict,
        provider_id=provider_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return page_of(schemas.CaseOut, page)


@router.get("/{case_id}", response_model=schemas.CaseDetailOut)
def get_case(case_id: uuid.UUID, session: SessionDep, _user: CurrentUser) -> schemas.CaseDetailOut:
    """The case with its provider, the record it was opened on, and the approving match."""
    case = CaseRepository(session).get(case_id)
    if case is None:
        raise NotFoundError(f"no case {case_id}")
    return presenters.case_detail(session, case)


@router.get("/{case_id}/audit", response_model=schemas.Page[schemas.AuditOut])
def case_audit(
    case_id: uuid.UUID,
    session: SessionDep,
    _user: CurrentUser,
    limit: Limit = 50,
    offset: Offset = 0,
) -> schemas.Page[schemas.AuditOut]:
    """Every recorded event on this case, newest first."""
    if CaseRepository(session).get(case_id) is None:
        raise NotFoundError(f"no case {case_id}")
    page = AuditRepository(session).for_entity("case", str(case_id), limit=limit, offset=offset)
    return page_of(schemas.AuditOut, page, [presenters.audit_out(r) for r in page.items])


@router.post(
    "/{case_id}/close",
    response_model=schemas.CaseOut,
    responses={409: {"description": "The case is not active."}},
)
def close(
    case_id: uuid.UUID, body: schemas.CloseCaseIn, session: SessionDep, actor: AdminActor
) -> schemas.CaseOut:
    """Close an active case, with a reason. Admin only."""
    case = close_case(session, actor, case_id, reason=body.reason)
    session.commit()
    return schemas.CaseOut.model_validate(case)


__all__ = ["router"]
