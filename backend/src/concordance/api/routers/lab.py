"""`/lab` - the robustness sweep and the LLM cost experiment.

Reading is open to every signed-in user: the curves are evidence about the
engine, and an analyst deciding whether to trust a confidence score is exactly
who should see the reliability diagram. Starting an experiment is admin-only,
because a sweep takes minutes of the worker's CPU and an LLM run spends the
provider's rate limit that reconciliation runs also need.

Both `POST`s return `202 Accepted` with the experiment in `QUEUED`, like a
reconciliation run; the page polls `GET /lab/results`, whose `live` field
carries the progress.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy.orm import Session

from concordance.api import schemas
from concordance.api.deps import AdminActor, CurrentUser, SessionDep, SettingsDep
from concordance.db.models import LabSweep
from concordance.lab import service

router = APIRouter(prefix="/lab", tags=["lab"])


def _run_out(session: Session, row: LabSweep | None) -> schemas.LabRunOut | None:
    if row is None:
        return None
    out = schemas.LabRunOut.model_validate(row)
    effective, error = service.effective_status(session, row)
    return out.model_copy(update={"status": effective, "error": error})


@router.get("/results", response_model=schemas.LabResultsOut)
def results(
    session: SessionDep,
    settings: SettingsDep,
    _user: CurrentUser,
    sweep_id: Annotated[
        uuid.UUID | None, Query(description="Omitted: the newest completed sweep.")
    ] = None,
) -> schemas.LabResultsOut:
    """Every number the Lab page draws, for one sweep and the LLM run that extends it."""
    found = service.results(session, settings, sweep_id)
    return schemas.LabResultsOut(
        sweep=_run_out(session, found["sweep"]),
        llm_run=_run_out(session, found["llm_run"]),
        live=_run_out(session, found["live"]),
        cells=[schemas.LabCellOut.model_validate(c) for c in found["cells"]],
        calibration=[schemas.LabCalibrationOut.model_validate(c) for c in found["calibration"]],
        llm=[schemas.LabLlmLevelOut.model_validate(c) for c in found["llm"]],
        price=schemas.LabPriceOut.model_validate(found["price"]),
        llm_enabled=found["llm_enabled"],
    )


@router.get("/experiments", response_model=list[schemas.LabRunOut])
def experiments(
    session: SessionDep,
    _user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[schemas.LabRunOut]:
    """Recent experiments of both kinds, newest first."""
    rows = service.list_experiments(session, limit)
    return [out for row in rows if (out := _run_out(session, row)) is not None]


@router.post(
    "/sweep",
    response_model=schemas.LabRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "Another Lab experiment is queued or running."}},
)
def sweep(
    body: schemas.LabSweepIn,
    session: SessionDep,
    settings: SettingsDep,
    actor: AdminActor,
) -> schemas.LabRunOut:
    """Queue a corruption sweep. Admin only."""
    row = service.request_sweep(
        session,
        settings,
        actor,
        levels=body.levels,
        providers=body.providers,
        sanctions=body.sanctions,
        seed=body.seed,
    )
    session.commit()
    out = _run_out(session, row)
    assert out is not None
    return out


@router.post(
    "/llm",
    response_model=schemas.LabRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"description": "No completed sweep to extend."},
        409: {"description": "The LLM is disabled, or another experiment is live."},
    },
)
def llm(
    body: schemas.LabLlmIn,
    session: SessionDep,
    settings: SettingsDep,
    actor: AdminActor,
) -> schemas.LabRunOut:
    """Queue the routed-versus-everything LLM sample on a finished sweep. Admin only."""
    row = service.request_llm(
        session,
        settings,
        actor,
        sweep_id=body.sweep_id,
        levels=body.levels,
        sample=body.sample,
    )
    session.commit()
    out = _run_out(session, row)
    assert out is not None
    return out


__all__ = ["router"]
