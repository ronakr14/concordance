"""Uploaded files, their column mappings, and the rows inside them (Q1)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from concordance.db.enums import SanctionFileStatus
from concordance.db.models import ColumnMapping, SanctionFile, SanctionRecord
from concordance.db.repositories.base import Page, paginate


class SanctionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- files ------------------------------------------------------------
    def file_by_hash(self, sha256: str) -> SanctionFile | None:
        """The same bytes uploaded twice are the same file, not a second one."""
        return self.session.scalar(select(SanctionFile).where(SanctionFile.sha256 == sha256))

    def get_file(self, file_id: uuid.UUID) -> SanctionFile | None:
        return self.session.get(SanctionFile, file_id)

    def list_files(
        self, status: str | None = None, *, limit: int | None = None, offset: int = 0
    ) -> Page[SanctionFile]:
        stmt = select(SanctionFile).order_by(SanctionFile.uploaded_at.desc())
        if status:
            stmt = stmt.where(SanctionFile.status == status)
        return paginate(self.session, stmt, limit, offset)

    def set_file_status(self, file_id: uuid.UUID, status: SanctionFileStatus | str) -> None:
        file = self.session.get(SanctionFile, file_id)
        if file is not None:
            file.status = str(status)

    # -- mappings ---------------------------------------------------------
    def default_mapping(self, source_authority: str) -> ColumnMapping | None:
        """The mapping to pre-fill the upload wizard with, if this source has one."""
        return self.session.scalar(
            select(ColumnMapping)
            .where(
                ColumnMapping.source_authority == source_authority,
                ColumnMapping.is_default.is_(True),
            )
            .order_by(ColumnMapping.created_at.desc())
        )

    def list_mappings(self, source_authority: str | None = None) -> list[ColumnMapping]:
        stmt = select(ColumnMapping).order_by(ColumnMapping.source_authority, ColumnMapping.name)
        if source_authority:
            stmt = stmt.where(ColumnMapping.source_authority == source_authority)
        return list(self.session.scalars(stmt))

    # -- records ----------------------------------------------------------
    def get_record(self, record_id: str) -> SanctionRecord | None:
        return self.session.scalar(
            select(SanctionRecord).where(SanctionRecord.record_id == record_id)
        )

    def records_for_file(
        self, file_id: uuid.UUID, *, limit: int | None = None, offset: int = 0
    ) -> Page[SanctionRecord]:
        stmt = (
            select(SanctionRecord)
            .where(SanctionRecord.file_id == file_id)
            .order_by(SanctionRecord.ordinal)
        )
        return paginate(self.session, stmt, limit, offset)

    def search_records(
        self,
        *,
        q: str | None = None,
        file_id: uuid.UUID | None = None,
        source_authority: str | None = None,
        state: str | None = None,
        sanction_type: str | None = None,
        is_organization: bool | None = None,
        include_history: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> Page[SanctionRecord]:
        """The sanctions list: current versions only, unless history is asked for.

        `q` matches the normalized name, the record key and the NPI. The name
        is matched on its normalized form - the same folding the engine uses -
        so the list and the results agree about who is who.
        """
        stmt = select(SanctionRecord).order_by(SanctionRecord.ordinal, SanctionRecord.id)
        if not include_history:
            stmt = stmt.where(SanctionRecord.is_current.is_(True))
        if file_id is not None:
            stmt = stmt.where(SanctionRecord.file_id == file_id)
        if source_authority:
            stmt = stmt.where(SanctionRecord.source_authority == source_authority)
        if state:
            stmt = stmt.where(SanctionRecord.state == state.upper())
        if sanction_type:
            stmt = stmt.where(SanctionRecord.sanction_type == sanction_type)
        if is_organization is not None:
            stmt = stmt.where(SanctionRecord.is_organization.is_(is_organization))
        if q and q.strip():
            text = q.strip()
            stmt = stmt.where(
                or_(
                    SanctionRecord.name_norm.contains(text.upper(), autoescape=True),
                    SanctionRecord.record_id.icontains(text, autoescape=True),
                    SanctionRecord.npi.contains(text, autoescape=True),
                )
            )
        return paginate(self.session, stmt, limit, offset)

    def facets(self) -> dict[str, list[str]]:
        """Distinct filter values over current records, for the filter bars.

        Offered from the data rather than hard-coded, because the sources are
        generic (Q1): a state board's exclusion types are not LEIE's.
        """

        def distinct(column: Any) -> list[str]:
            stmt = (
                select(column)
                .where(SanctionRecord.is_current.is_(True), column.is_not(None), column != "")
                .distinct()
                .order_by(column)
            )
            return [str(v) for v in self.session.scalars(stmt)]

        return {
            "sanction_types": distinct(SanctionRecord.sanction_type),
            "source_authorities": distinct(SanctionRecord.source_authority),
            "states": distinct(SanctionRecord.state),
        }

    def get_record_row(self, row_id: uuid.UUID) -> SanctionRecord | None:
        """By surrogate id. `get_record` takes the business key, which is not unique
        once a record has more than one version."""
        return self.session.get(SanctionRecord, row_id)

    def count(self) -> int:
        from sqlalchemy import func

        return int(self.session.scalar(select(func.count()).select_from(SanctionRecord)) or 0)


__all__ = ["SanctionRepository"]
