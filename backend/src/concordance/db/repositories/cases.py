"""Cases: the review workload, and the conflict surface (Q3, Q5)."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from concordance.db.enums import CaseStatus
from concordance.db.models import Case
from concordance.db.repositories.base import Page, paginate


class CaseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, case_id: uuid.UUID) -> Case | None:
        return self.session.get(Case, case_id)

    def by_number(self, case_number: str) -> Case | None:
        return self.session.scalar(select(Case).where(Case.case_number == case_number))

    def active_for(self, provider_id: str, sanction_record_id: uuid.UUID) -> Case | None:
        """The one the partial unique index allows, if it exists."""
        return self.session.scalar(
            select(Case).where(
                Case.provider_id == provider_id,
                Case.sanction_record_id == sanction_record_id,
                Case.status == CaseStatus.ACTIVE,
            )
        )

    def list_cases(
        self,
        *,
        status: str | None = None,
        conflicts_only: bool = False,
        provider_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Page[Case]:
        """Cases, newest first. The date range is over `start_date`."""
        stmt = select(Case).order_by(Case.created_at.desc(), Case.id)
        if status:
            stmt = stmt.where(Case.status == status)
        if conflicts_only:
            stmt = stmt.where(Case.conflict_flag.is_(True))
        if provider_id:
            stmt = stmt.where(Case.provider_id == provider_id)
        if date_from is not None:
            stmt = stmt.where(Case.start_date >= date_from)
        if date_to is not None:
            stmt = stmt.where(Case.start_date <= date_to)
        return paginate(self.session, stmt, limit, offset)

    def due_for_expiry(self, today: date) -> list[Case]:
        """Active cases whose window has closed. The scheduled job's input (Q3)."""
        return list(
            self.session.scalars(
                select(Case)
                .where(Case.status == CaseStatus.ACTIVE, Case.end_date < today)
                .order_by(Case.end_date)
            )
        )

    def flag_conflict(self, case_id: uuid.UUID, match_result_id: uuid.UUID) -> None:
        """A later run disagreed. Surface it; do not close the case (Q5).

        Closing automatically would let a re-run silently retract a decision a
        human made. The flag puts it in front of that human instead.
        """
        self.session.execute(
            update(Case)
            .where(Case.id == case_id)
            .values(conflict_flag=True, conflict_match_result_id=match_result_id)
        )


__all__ = ["CaseRepository"]
