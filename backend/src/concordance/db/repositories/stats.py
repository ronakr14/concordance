"""The dashboard's numbers. Each is one grouped query over an index.

Every count here is over *current* results - `superseded_by IS NULL` - which the
partial indexes on `match_results` serve directly. A dashboard that counted
superseded rows would report a record as matched three times after three runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Float, cast, func, literal, select
from sqlalchemy.orm import Session

from concordance.db.enums import CaseStatus, Decision, ReviewStatus
from concordance.db.models import Case, MatchResult, Provider, ReconciliationRun, SanctionRecord

#: Confidence histogram resolution. Ten bins is what a reviewer can read.
CONFIDENCE_BINS = 10

#: Buckets the volume series may be grouped by. Anything else never reaches SQL.
VOLUME_BUCKETS = ("day", "week", "month")


@dataclass(frozen=True, slots=True)
class Kpis:
    providers: int
    sanction_records: int
    matched: int
    ambiguous: int
    no_match: int
    pending_review: int
    escalated: int
    approved: int
    rejected: int
    cases_active: int
    cases_expired: int
    cases_closed: int
    conflicts: int


class StatsRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def kpis(self) -> Kpis:
        current = MatchResult.superseded_by.is_(None)
        decisions = self._grouped(
            select(MatchResult.decision, func.count()).where(current).group_by(MatchResult.decision)
        )
        # Pending means awaiting a person: a proposed match or an ambiguous
        # one. A NO_MATCH nobody has looked at is not a review backlog.
        reviews = self._grouped(
            select(MatchResult.review_status, func.count())
            .where(current, MatchResult.decision != Decision.NO_MATCH)
            .group_by(MatchResult.review_status)
        )
        cases = self._grouped(select(Case.status, func.count()).group_by(Case.status))
        conflicts = self.session.scalar(
            select(func.count())
            .select_from(Case)
            .where(Case.conflict_flag.is_(True), Case.status == CaseStatus.ACTIVE)
        )
        return Kpis(
            providers=self._count(select(func.count()).select_from(Provider)),
            sanction_records=self._count(
                select(func.count())
                .select_from(SanctionRecord)
                .where(SanctionRecord.is_current.is_(True))
            ),
            matched=int(decisions.get(Decision.MATCH, 0)),
            ambiguous=int(decisions.get(Decision.AMBIGUOUS, 0)),
            no_match=int(decisions.get(Decision.NO_MATCH, 0)),
            pending_review=int(reviews.get(ReviewStatus.PENDING, 0)),
            escalated=int(reviews.get(ReviewStatus.ESCALATED, 0)),
            approved=int(reviews.get(ReviewStatus.APPROVED, 0)),
            rejected=int(reviews.get(ReviewStatus.REJECTED, 0)),
            cases_active=int(cases.get(CaseStatus.ACTIVE, 0)),
            cases_expired=int(cases.get(CaseStatus.EXPIRED, 0)),
            cases_closed=int(cases.get(CaseStatus.CLOSED, 0)),
            conflicts=int(conflicts or 0),
        )

    def confidence_distribution(self, *, decision: str | None = None) -> list[tuple[str, int]]:
        """Calibrated confidence in ten equal bins, empty bins included."""
        # `width_bucket` puts exactly 1.0 in bin 11, so the upper bound is
        # nudged past it; a confidence of 1.0 belongs in the top bin.
        bucket = func.width_bucket(
            cast(MatchResult.calibrated_confidence, Float), 0.0, 1.0 + 1e-9, CONFIDENCE_BINS
        )
        stmt = (
            select(bucket.label("bin"), func.count())
            .where(
                MatchResult.superseded_by.is_(None),
                MatchResult.calibrated_confidence.is_not(None),
            )
            .group_by("bin")
        )
        if decision:
            stmt = stmt.where(MatchResult.decision == decision)
        counts = {int(b): int(n) for b, n in self.session.execute(stmt).all()}
        width = 1.0 / CONFIDENCE_BINS
        return [
            (f"{(i - 1) * width:.1f}-{i * width:.1f}", counts.get(i, 0))
            for i in range(1, CONFIDENCE_BINS + 1)
        ]

    def state_distribution(self, *, decision: str | None = None) -> list[tuple[str, int]]:
        """Current results by the sanctioned party's state, largest first."""
        state = func.coalesce(SanctionRecord.state, literal("??"))
        stmt = (
            select(state.label("state"), func.count().label("n"))
            .select_from(MatchResult)
            .join(SanctionRecord, SanctionRecord.id == MatchResult.sanction_record_id)
            .where(MatchResult.superseded_by.is_(None))
            .group_by("state")
            .order_by(func.count().desc(), state)
        )
        if decision:
            stmt = stmt.where(MatchResult.decision == decision)
        return [(str(s), int(n)) for s, n in self.session.execute(stmt).all()]

    def case_status(self) -> list[tuple[str, int]]:
        counts = self._grouped(select(Case.status, func.count()).group_by(Case.status))
        return [(str(status), counts.get(status, 0)) for status in CaseStatus]

    def reconciliation_volume(self, *, days: int = 30, bucket: str = "day") -> list[dict[str, Any]]:
        """Runs and records reconciled per period, over the last `days` days."""
        if bucket not in VOLUME_BUCKETS:
            raise ValueError(f"bucket must be one of {VOLUME_BUCKETS}")
        since = datetime.now(UTC) - timedelta(days=days)
        period = func.date_trunc(bucket, ReconciliationRun.created_at)
        stmt = (
            select(
                period.label("period"),
                func.count().label("runs"),
                func.coalesce(func.sum(ReconciliationRun.records_total), 0).label("records"),
                func.coalesce(func.sum(ReconciliationRun.matched_count), 0).label("matched"),
                func.coalesce(func.sum(ReconciliationRun.llm_cost_usd), 0).label("cost"),
            )
            .where(ReconciliationRun.created_at >= since)
            .group_by("period")
            .order_by("period")
        )
        return [
            {
                "period": p.date().isoformat(),
                "runs": int(runs),
                "records": int(records),
                "matched": int(matched),
                "llm_cost_usd": float(cost),
            }
            for p, runs, records, matched, cost in self.session.execute(stmt).all()
        ]

    def _grouped(self, stmt: Any) -> dict[str, int]:
        return {str(key): int(n) for key, n in self.session.execute(stmt).all()}

    def _count(self, stmt: Any) -> int:
        return int(self.session.scalar(stmt) or 0)


__all__ = ["CONFIDENCE_BINS", "VOLUME_BUCKETS", "Kpis", "StatsRepository"]
