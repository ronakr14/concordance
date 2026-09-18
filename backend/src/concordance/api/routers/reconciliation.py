"""`/reconciliation` - start a run, watch it, cancel it.

`POST /reconciliation/run` returns `202 Accepted` with the run in `QUEUED` and
does no matching itself: the worker does, in its own process. The client polls
`GET /reconciliation/runs/{id}`, whose counts move as each chunk commits.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from concordance.api import schemas
from concordance.api.deps import ActorDep, CurrentUser, SessionDep
from concordance.api.errors import NotFoundError
from concordance.api.routers.common import Limit, Offset, page_of
from concordance.db.repositories.matches import MatchRepository
from concordance.jobs.runs import cancel_run, start_run

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.post(
    "/run",
    response_model=schemas.RunOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "A run over the same records is already queued or running, "
                                    "or the file is not committed or has been superseded."}},
)
def run(body: schemas.RunIn, session: SessionDep, actor: ActorDep) -> schemas.RunOut:
    """Queue a reconciliation run and return it immediately."""
    queued = start_run(
        session,
        actor,
        strategy=body.strategy,
        scoring_config_id=body.scoring_config_id,
        file_id=body.file_id,
        limit=body.limit,
        max_candidates=body.max_candidates,
    )
    session.commit()
    return schemas.RunOut.model_validate(queued)


@router.get("/runs", response_model=schemas.Page[schemas.RunOut])
def list_runs(
    session: SessionDep,
    _user: CurrentUser,
    limit: Limit = 50,
    offset: Offset = 0,
    status_: Annotated[str | None, Query(alias="status")] = None,
    file_id: uuid.UUID | None = None,
) -> schemas.Page[schemas.RunOut]:
    page = MatchRepository(session).list_runs(
        status=status_, file_id=file_id, limit=limit, offset=offset
    )
    return page_of(schemas.RunOut, page)


@router.get("/runs/{run_id}", response_model=schemas.RunOut)
def get_run(run_id: uuid.UUID, session: SessionDep, _user: CurrentUser) -> schemas.RunOut:
    """Status, progress counts, provenance and cost."""
    found = MatchRepository(session).get_run(run_id)
    if found is None:
        raise NotFoundError(f"no run {run_id}")
    return schemas.RunOut.model_validate(found)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=schemas.RunOut,
    responses={409: {"description": "The run has already finished."}},
)
def cancel(run_id: uuid.UUID, session: SessionDep, actor: ActorDep) -> schemas.RunOut:
    """Cancel a queued run outright, or stop a running one after its current chunk."""
    cancelled = cancel_run(session, actor, run_id)
    session.commit()
    return schemas.RunOut.model_validate(cancelled)


__all__ = ["router"]
