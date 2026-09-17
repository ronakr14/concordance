"""Runs, results and candidates.

Every list method filters `superseded_by IS NULL` unless the caller asks for
history explicitly (Q5). That default is the reason supersede is safe: a
re-reconciliation adds rows rather than editing them, and nothing in the UI has
to remember to exclude the old ones.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from concordance.db.models import (
    MatchCandidate,
    MatchResult,
    ReconciliationRun,
    ScoringConfig,
)
from concordance.db.repositories.base import Page, paginate


class MatchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- runs -------------------------------------------------------------
    def get_run(self, run_id: uuid.UUID) -> ReconciliationRun | None:
        return self.session.get(ReconciliationRun, run_id)

    def list_runs(self, *, limit: int | None = None, offset: int = 0) -> Page[ReconciliationRun]:
        stmt = select(ReconciliationRun).order_by(ReconciliationRun.created_at.desc())
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


__all__ = ["MatchRepository"]
