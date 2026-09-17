"""Uploaded files, their column mappings, and the rows inside them (Q1)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
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

    def count(self) -> int:
        from sqlalchemy import func

        return int(self.session.scalar(select(func.count()).select_from(SanctionRecord)) or 0)


__all__ = ["SanctionRepository"]
