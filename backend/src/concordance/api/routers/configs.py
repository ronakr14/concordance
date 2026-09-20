"""`/scoring-configs` - versions, lineage, retune and activation.

Reading is open to every signed-in user: an analyst looking at a confidence
score is entitled to know which config produced it and how that config was
measured. Retuning and activating are admin-only, and they are two calls on
purpose - a retune proposes a version with its holdout numbers beside its
parent's, and somebody decides whether those numbers justify switching.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from concordance.api import schemas
from concordance.api.deps import AdminActor, CurrentUser, SessionDep, SettingsDep
from concordance.db.models import ConfigActivation, ScoringConfig
from concordance.errors import NotFoundError
from concordance.learning import service

router = APIRouter(prefix="/scoring-configs", tags=["scoring-configs"])


def _summary(session: SessionDep, config_id: uuid.UUID) -> schemas.ScoringConfigOut:
    found = next((c for c in service.config_summaries(session) if c["id"] == config_id), None)
    if found is None:
        raise NotFoundError(f"no scoring config {config_id}")
    return schemas.ScoringConfigOut.model_validate(found)


@router.get("", response_model=list[schemas.ScoringConfigOut])
def list_configs(session: SessionDep, _user: CurrentUser) -> list[schemas.ScoringConfigOut]:
    """Every version, newest first, with lineage, metrics, run counts and the active flag."""
    return [schemas.ScoringConfigOut.model_validate(c) for c in service.config_summaries(session)]


@router.get("/activations", response_model=list[schemas.ConfigActivationOut])
def activations(
    session: SessionDep,
    _user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[schemas.ConfigActivationOut]:
    """Which config was live when, newest first, and who switched it."""
    rows = session.execute(
        select(ConfigActivation, ScoringConfig.version)
        .join(ScoringConfig, ScoringConfig.id == ConfigActivation.scoring_config_id)
        .order_by(ConfigActivation.created_at.desc(), ConfigActivation.id.desc())
        .limit(limit)
    ).all()
    return [
        schemas.ConfigActivationOut(
            id=a.id,
            scoring_config_id=a.scoring_config_id,
            version=version,
            activated_by=a.activated_by,
            reason=a.reason,
            created_at=a.created_at,
        )
        for a, version in rows
    ]


@router.post(
    "/retune",
    response_model=schemas.RetuneOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "No active config, or no run with a pair tally yet."},
        422: {"description": "Not enough labels: fewer than RETUNE_MIN_LABELS, or one class only."},
    },
)
def retune(
    body: schemas.RetuneIn,
    session: SessionDep,
    settings: SettingsDep,
    actor: AdminActor,
) -> schemas.RetuneOut:
    """Retune the active config on every reviewer label so far. Admin only.

    Writes a new version whose parent is the active one; never edits either.
    Takes seconds - the fit runs over a run's distinct comparison vectors, not
    its pairs - so it answers directly rather than through a job.
    """
    retuned = service.retune_active(
        session, settings, actor, run_id=body.run_id, activate=body.activate
    )
    session.commit()
    return schemas.RetuneOut(
        config=_summary(session, retuned.row.id),
        improved=retuned.result.improved,
        recommended=retuned.result.recommended,
        verdict=retuned.result.verdict,
        activated=retuned.activated,
    )


@router.post(
    "/{config_id}/activate",
    response_model=schemas.ScoringConfigOut,
    responses={409: {"description": "Already the active config."}},
)
def activate(
    config_id: uuid.UUID,
    body: schemas.ActivateIn,
    session: SessionDep,
    actor: AdminActor,
) -> schemas.ScoringConfigOut:
    """Make a version the one new runs score with. Admin only; audited."""
    service.activate_config(session, actor, config_id, reason=body.reason)
    session.commit()
    return _summary(session, config_id)


__all__ = ["router"]
