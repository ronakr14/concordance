"""Provider master reads. Written only by the loader, so there is no create."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from concordance.db.models import Provider
from concordance.db.repositories.base import Page, paginate


class ProviderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, provider_id: str) -> Provider | None:
        return self.session.scalar(select(Provider).where(Provider.provider_id == provider_id))

    def get_many(self, provider_ids: list[str]) -> dict[str, Provider]:
        """One query for N ids. The Investigation view needs a whole candidate set."""
        if not provider_ids:
            return {}
        rows = self.session.scalars(select(Provider).where(Provider.provider_id.in_(provider_ids)))
        return {p.provider_id: p for p in rows}

    def by_npi(self, npi: str) -> list[Provider]:
        return list(self.session.scalars(select(Provider).where(Provider.npi == npi)))

    def search(
        self, query: str | None = None, *, limit: int | None = None, offset: int = 0
    ) -> Page[Provider]:
        """Name or id search for the provider list.

        Matches on the normalized name rather than the display name so that
        "o'brien" finds "OBrien" - the same folding the engine matches on, not a
        second, looser rule that would make the UI disagree with the results.
        """
        stmt = select(Provider).order_by(Provider.ordinal)
        if query:
            needle = f"%{query.strip().upper()}%"
            stmt = stmt.where(
                or_(
                    Provider.name_norm.like(needle),
                    Provider.provider_id.like(needle),
                    Provider.npi.like(needle),
                )
            )
        return paginate(self.session, stmt, limit, offset)

    def count(self) -> int:
        from sqlalchemy import func

        return int(self.session.scalar(select(func.count()).select_from(Provider)) or 0)


__all__ = ["ProviderRepository"]
