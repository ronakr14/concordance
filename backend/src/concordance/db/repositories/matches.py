"""Runs, results and candidates.

Every list method filters `superseded_by IS NULL` unless the caller asks for
history explicitly (Q5). That default is the reason supersede is safe: a
re-reconciliation adds rows rather than editing them, and nothing in the UI has
to remember to exclude the old ones.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from concordance.db.enums import CaseStatus
from concordance.db.models import (
    Case,
    MatchCandidate,
    MatchResult,
    ReconciliationRun,
    SanctionRecord,
    ScoringConfig,
)
from concordance.db.repositories.base import Page, paginate, paginate_rows


@dataclass(frozen=True, slots=True)
class MatchFilter:
    """Everything the review queue filters on. `None` leaves a filter off."""

    run_id: uuid.UUID | None = None
    decision: str | None = None
    review_status: str | None = None
    min_confidence: float | None = None
    max_confidence: float | None = None
    state: str | None = None
    sanction_type: str | None = None
    is_organization: bool | None = None
    conflict: bool | None = None
    date_from: date | None = None
    date_to: date | None = None
    include_superseded: bool = False
    sort: str = "confidence"
    descending: bool = True


#: Sort keys the API accepts. A column name from a query string never reaches
#: `order_by` directly.
SORTS = ("confidence", "date")


class MatchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- runs -------------------------------------------------------------
    def get_run(self, run_id: uuid.UUID) -> ReconciliationRun | None:
        return self.session.get(ReconciliationRun, run_id)

    def list_runs(
        self,
        *,
        status: str | None = None,
        file_id: uuid.UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Page[ReconciliationRun]:
        stmt = select(ReconciliationRun).order_by(
            ReconciliationRun.created_at.desc(), ReconciliationRun.id
        )
        if status:
            stmt = stmt.where(ReconciliationRun.status == status)
        if file_id is not None:
            stmt = stmt.where(ReconciliationRun.file_id == file_id)
        return paginate(self.session, stmt, limit, offset)

    def latest_run(self) -> ReconciliationRun | None:
        return self.session.scalar(
            select(ReconciliationRun).order_by(ReconciliationRun.created_at.desc()).limit(1)
        )

    # -- configs ----------------------------------------------------------
    def get_config(self, version: str) -> ScoringConfig | None:
        return self.session.scalar(select(ScoringConfig).where(ScoringConfig.version == version))

    def latest_config(self) -> ScoringConfig | None:
        return self.session.scalar(
            select(ScoringConfig).order_by(ScoringConfig.fitted_at.desc()).limit(1)
        )

    # -- results ----------------------------------------------------------
    def get_result(self, result_id: uuid.UUID) -> MatchResult | None:
        return self.session.get(MatchResult, result_id)

    def list_results(
        self,
        run_id: uuid.UUID | None = None,
        *,
        decision: str | None = None,
        review_status: str | None = None,
        include_superseded: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> Page[MatchResult]:
        stmt = select(MatchResult).order_by(MatchResult.created_at.desc())
        if not include_superseded:
            stmt = stmt.where(MatchResult.superseded_by.is_(None))
        if run_id is not None:
            stmt = stmt.where(MatchResult.run_id == run_id)
        if decision:
            stmt = stmt.where(MatchResult.decision == decision)
        if review_status:
            stmt = stmt.where(MatchResult.review_status == review_status)
        return paginate(self.session, stmt, limit, offset)

    def search(
        self, where: MatchFilter, *, limit: int | None = None, offset: int = 0
    ) -> Page[tuple[MatchResult, SanctionRecord]]:
        """The review queue: results beside the record they decide, filtered.

        Current results only unless `include_superseded` (Q5). `conflict`
        selects results caught up in a conflict on a live case - the result the
        case was opened on, or the later one that disagreed with it.
        """
        stmt = select(MatchResult, SanctionRecord).join(
            SanctionRecord, SanctionRecord.id == MatchResult.sanction_record_id
        )
        if not where.include_superseded:
            stmt = stmt.where(MatchResult.superseded_by.is_(None))
        if where.run_id is not None:
            stmt = stmt.where(MatchResult.run_id == where.run_id)
        if where.decision:
            stmt = stmt.where(MatchResult.decision == where.decision)
        if where.review_status:
            stmt = stmt.where(MatchResult.review_status == where.review_status)
        if where.min_confidence is not None:
            stmt = stmt.where(MatchResult.calibrated_confidence >= where.min_confidence)
        if where.max_confidence is not None:
            stmt = stmt.where(MatchResult.calibrated_confidence <= where.max_confidence)
        if where.state:
            stmt = stmt.where(SanctionRecord.state == where.state.upper())
        if where.sanction_type:
            stmt = stmt.where(SanctionRecord.sanction_type == where.sanction_type)
        if where.is_organization is not None:
            stmt = stmt.where(SanctionRecord.is_organization.is_(where.is_organization))
        if where.date_from is not None:
            stmt = stmt.where(
                MatchResult.created_at >= datetime.combine(where.date_from, time.min, UTC)
            )
        if where.date_to is not None:
            # Inclusive of the whole last day.
            stmt = stmt.where(
                MatchResult.created_at
                < datetime.combine(where.date_to + timedelta(days=1), time.min, UTC)
            )
        if where.conflict is not None:
            live_conflict = (Case.conflict_flag.is_(True), Case.status == CaseStatus.ACTIVE)
            disagreeing = select(Case.conflict_match_result_id).where(*live_conflict)
            opened_on = select(Case.match_result_id).where(*live_conflict)
            involved = or_(MatchResult.id.in_(disagreeing), MatchResult.id.in_(opened_on))
            stmt = stmt.where(involved if where.conflict else ~involved)

        key = MatchResult.created_at if where.sort == "date" else MatchResult.calibrated_confidence
        # Nulls count as the lowest value in both directions: last when the
        # most confident come first, first when the least confident do. That
        # is also exactly the order the confidence index can be walked in.
        ordered = key.desc().nulls_last() if where.descending else key.asc().nulls_first()
        # The id breaks ties: without it, rows of equal confidence can swap
        # between pages, and a reviewer sees one twice and another never.
        stmt = stmt.order_by(ordered, MatchResult.id)
        return paginate_rows(self.session, stmt, limit, offset)

    def history_for_record(self, sanction_record_id: uuid.UUID) -> list[MatchResult]:
        """Every decision ever made about this record, newest first.

        The one place superseded rows are wanted: "what did we decide, and when
        did it change" is the question this answers.
        """
        return list(
            self.session.scalars(
                select(MatchResult)
                .where(MatchResult.sanction_record_id == sanction_record_id)
                .order_by(MatchResult.created_at.desc())
            )
        )

    def current_for_record(self, sanction_record_id: uuid.UUID) -> MatchResult | None:
        return self.session.scalar(
            select(MatchResult).where(
                MatchResult.sanction_record_id == sanction_record_id,
                MatchResult.superseded_by.is_(None),
            )
        )

    def supersede(self, old_id: uuid.UUID, new_id: uuid.UUID) -> None:
        """Point an old result at the one that replaced it. Never edits the decision."""
        self.session.execute(
            update(MatchResult)
            .where(MatchResult.id == old_id)
            .values(superseded_by=new_id, superseded_at=datetime.now(UTC))
        )

    # -- candidates -------------------------------------------------------
    def candidates_for(self, match_result_id: uuid.UUID) -> list[MatchCandidate]:
        return list(
            self.session.scalars(
                select(MatchCandidate)
                .where(MatchCandidate.match_result_id == match_result_id)
                .order_by(MatchCandidate.rank)
            )
        )


__all__ = ["SORTS", "MatchFilter", "MatchRepository"]
