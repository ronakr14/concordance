"""Ground truth, evaluation runs, and reviewer feedback.

These three tables are what keep the system measurable after it leaves the
laptop. `ground_truth` is synthetic-only today; `feedback_events` is the real
thing's replacement for it, and both carry a comparison vector so a future
supervised refit reads from one shape rather than two.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from concordance.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from concordance.db.enums import (
    EvalStrategy,
    ExpectedOutcome,
    FeedbackLabel,
    check_values,
)


class GroundTruth(Base):
    """The generator's answer for one synthetic sanction record.

    Keyed by the sanction record rather than given its own surrogate id: there
    is exactly one truth per record, and a surrogate key would allow two.
    """

    __tablename__ = "ground_truth"

    sanction_record_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sanction_records.id", ondelete="CASCADE"),
        primary_key=True,
    )
    expected_provider_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("providers.provider_id", ondelete="SET NULL")
    )
    expected_outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Which corruption operations the generator applied, and with what
    #: parameters - the per-record provenance behind a scenario's metrics.
    corruption_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    scenario_tag: Mapped[str] = mapped_column(String(50), nullable=False, server_default="")

    __table_args__ = (
        check_values("expected_outcome", ExpectedOutcome),
        Index("ix_ground_truth_scenario_tag", "scenario_tag"),
        Index("ix_ground_truth_expected_provider_id", "expected_provider_id"),
    )


class EvalRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One strategy measured at one corruption level."""

    __tablename__ = "eval_runs"

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("reconciliation_runs.id", ondelete="SET NULL")
    )
    corruption_level: Mapped[float | None] = mapped_column(Float)
    strategy: Mapped[str] = mapped_column(String(30), nullable=False)
    precision: Mapped[float | None] = mapped_column(Float)
    recall: Mapped[float | None] = mapped_column(Float)
    f1: Mapped[float | None] = mapped_column(Float)
    false_positives: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    false_negatives: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    brier: Mapped[float | None] = mapped_column(Float)
    ece: Mapped[float | None] = mapped_column(Float)
    #: The reliability diagram's bins, as rendered - stored so a report can be
    #: redrawn without rerunning the evaluation that produced it.
    reliability_bins: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    blocking_recall: Mapped[float | None] = mapped_column(Float)
    scoring_config_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("scoring_configs.id", ondelete="SET NULL")
    )

    __table_args__ = (
        check_values("strategy", EvalStrategy),
        Index("ix_eval_runs_strategy_corruption_level", "strategy", "corruption_level"),
    )


class FeedbackEvent(CreatedAtMixin, Base):
    """A reviewer's verdict on one match result.

    The comparison vector is copied in rather than referenced, for the same
    reason `match_candidates.field_weights` is stored: a label is only training
    data if the features it was assigned against are the features that were
    actually shown.
    """

    __tablename__ = "feedback_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_result_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("match_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    label: Mapped[str] = mapped_column(String(20), nullable=False)
    comparison_vector: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        check_values("label", FeedbackLabel),
        Index("ix_feedback_events_match_result_id", "match_result_id"),
    )


__all__ = ["EvalRun", "FeedbackEvent", "GroundTruth"]
