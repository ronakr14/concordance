"""`/audit` and `/stats/*` - reading the trail, and the dashboard's numbers.

`GET /audit` is admin-only: it carries client addresses and every user's actions.
A case's own trail, which an analyst does need, is `GET /cases/{id}/audit`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from concordance.api import presenters, schemas
from concordance.api.deps import AdminUser, CurrentUser, SessionDep
from concordance.api.routers.common import Limit, Offset, page_of
from concordance.db.repositories.audit import AuditRepository
from concordance.db.repositories.stats import StatsRepository

audit_router = APIRouter(prefix="/audit", tags=["audit"])
stats_router = APIRouter(prefix="/stats", tags=["stats"])

DecisionQ = Literal["MATCH", "AMBIGUOUS", "NO_MATCH"]


@audit_router.get("", response_model=schemas.Page[schemas.AuditOut])
def search_audit(
    session: SessionDep,
    _admin: AdminUser,
    limit: Limit = 50,
    offset: Offset = 0,
    entity_type: Annotated[str | None, Query(max_length=50)] = None,
    entity_id: Annotated[str | None, Query(max_length=64)] = None,
    actor_user_id: uuid.UUID | None = None,
    action: Annotated[
        str | None, Query(max_length=100, description="Exact, or a family ending in `.`.")
    ] = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> schemas.Page[schemas.AuditOut]:
    page = AuditRepository(session).search(
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        action=action,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return page_of(schemas.AuditOut, page, [presenters.audit_out(r) for r in page.items])


@stats_router.get("/kpis", response_model=schemas.KpiOut)
def kpis(session: SessionDep, _user: CurrentUser) -> schemas.KpiOut:
    return schemas.KpiOut.model_validate(StatsRepository(session).kpis())


@stats_router.get("/confidence-distribution", response_model=list[schemas.BucketOut])
def confidence_distribution(
    session: SessionDep, _user: CurrentUser, decision: DecisionQ | None = None
) -> list[schemas.BucketOut]:
    rows = StatsRepository(session).confidence_distribution(decision=decision)
    return [schemas.BucketOut(label=label, count=n) for label, n in rows]


@stats_router.get("/state-distribution", response_model=list[schemas.BucketOut])
def state_distribution(
    session: SessionDep, _user: CurrentUser, decision: DecisionQ | None = None
) -> list[schemas.BucketOut]:
    rows = StatsRepository(session).state_distribution(decision=decision)
    return [schemas.BucketOut(label=label, count=n) for label, n in rows]


@stats_router.get("/case-status", response_model=list[schemas.BucketOut])
def case_status(session: SessionDep, _user: CurrentUser) -> list[schemas.BucketOut]:
    return [schemas.BucketOut(label=s, count=n) for s, n in StatsRepository(session).case_status()]


@stats_router.get("/reconciliation-volume", response_model=list[schemas.VolumePointOut])
def reconciliation_volume(
    session: SessionDep,
    _user: CurrentUser,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
    bucket: Literal["day", "week", "month"] = "day",
) -> list[schemas.VolumePointOut]:
    points = StatsRepository(session).reconciliation_volume(days=days, bucket=bucket)
    return [schemas.VolumePointOut(**p) for p in points]



__all__ = ["audit_router", "stats_router"]
