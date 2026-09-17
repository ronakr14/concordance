"""Ground truth, evaluation runs and reviewer feedback."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from concordance.db.models import EvalRun, FeedbackEvent, GroundTruth, SanctionRecord
from concordance.db.repositories.base import Page, paginate


class EvalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- ground truth -----------------------------------------------------
    def truth_for(self, sanction_record_id: uuid.UUID) -> GroundTruth | None:
        return self.session.get(GroundTruth, sanction_record_id)

    def truth_by_record_id(self, record_id: str) -> GroundTruth | None:
        """By the business key, which is what a report or a CLI argument carries."""
        return self.session.scalar(
            select(GroundTruth)
            .join(SanctionRecord, SanctionRecord.id == GroundTruth.sanction_record_id)
            .where(SanctionRecord.record_id == record_id)
        )

    def scenario_counts(self) -> dict[str, int]:
        from sqlalchemy import func

        rows = self.session.execute(
            select(GroundTruth.scenario_tag, func.count())
            .group_by(GroundTruth.scenario_tag)
            .order_by(GroundTruth.scenario_tag)
        )
        return {tag: int(count) for tag, count in rows}

    # -- eval runs --------------------------------------------------------
    def record_eval(self, **values: Any) -> EvalRun:
        row = EvalRun(**values)
        self.session.add(row)
        return row

    def list_evals(
        self, *, strategy: str | None = None, limit: int | None = None, offset: int = 0
    ) -> Page[EvalRun]:
        stmt = select(EvalRun).order_by(EvalRun.created_at.desc())
        if strategy:
            stmt = stmt.where(EvalRun.strategy == strategy)
        return paginate(self.session, stmt, limit, offset)

    # -- feedback ---------------------------------------------------------
    def add_feedback(
        self,
        *,
        match_result_id: uuid.UUID,
        reviewer_id: uuid.UUID | None,
        label: str,
        comparison_vector: dict[str, Any] | None = None,
    ) -> FeedbackEvent:
        row = FeedbackEvent(
            match_result_id=match_result_id,
            reviewer_id=reviewer_id,
            label=label,
            comparison_vector=comparison_vector or {},
        )
        self.session.add(row)
        return row

    def feedback_for_training(self, limit: int = 10_000) -> list[FeedbackEvent]:
        """Labelled examples for a supervised refit, oldest first.

        Oldest first so a refit reads a stable prefix: a run repeated tomorrow
        sees the same first N rows plus whatever arrived, rather than a
        different window of the same data.
        """
        return list(
            self.session.scalars(
                select(FeedbackEvent).order_by(FeedbackEvent.created_at).limit(limit)
            )
        )


__all__ = ["EvalRepository"]
