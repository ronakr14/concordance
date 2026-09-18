"""`/providers` - the provider directory and one provider's compliance history.

Read-only: the provider master is written by the loader and nothing else. Any
authenticated user may read it, because an analyst investigating a match needs
the provider's full record and every other decision that named them.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from concordance.api import presenters, schemas
from concordance.api.deps import CurrentUser, SessionDep
from concordance.api.errors import NotFoundError
from concordance.api.routers.common import Limit, Offset, page_of
from concordance.db.repositories.providers import ProviderFilter, ProviderRepository

router = APIRouter(prefix="/providers", tags=["providers"])

ComplianceQ = Literal["EXCLUDED", "UNDER_REVIEW", "CLEAR"]
RecordTypeQ = Literal["individual", "organization"]


@router.get("", response_model=schemas.Page[schemas.ProviderListItemOut])
def list_providers(
    session: SessionDep,
    _user: CurrentUser,
    limit: Limit = 50,
    offset: Offset = 0,
    q: Annotated[
        str | None,
        Query(max_length=200, description="A name substring, an exact provider id, or a 10-digit NPI."),
    ] = None,
    state: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    specialty: Annotated[str | None, Query(max_length=100)] = None,
    record_type: RecordTypeQ | None = None,
    compliance: ComplianceQ | None = None,
    sort: Literal["provider_id", "name", "state"] = "provider_id",
    order: Literal["asc", "desc"] = "asc",
) -> schemas.Page[schemas.ProviderListItemOut]:
    """The directory, each provider beside its derived compliance status."""
    where = ProviderFilter(
        query=q.strip() if q and q.strip() else None,
        state=state,
        specialty=specialty,
        is_organization=None if record_type is None else record_type == "organization",
        compliance=compliance,
        sort=sort,
        descending=order == "desc",
    )
    page = ProviderRepository(session).directory(where, limit=limit, offset=offset)
    items = [presenters.provider_item(row, status) for row, status in page.items]
    return page_of(schemas.ProviderListItemOut, page, items)


@router.get("/{provider_id}", response_model=schemas.ProviderDetailOut)
def get_provider(
    provider_id: str, session: SessionDep, _user: CurrentUser
) -> schemas.ProviderDetailOut:
    """One provider: the full record, every case, and every result that ranked them."""
    row = ProviderRepository(session).get(provider_id)
    if row is None:
        raise NotFoundError(f"no provider {provider_id}")
    return presenters.provider_detail(session, row)


__all__ = ["router"]
