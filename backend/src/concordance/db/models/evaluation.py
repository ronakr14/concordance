"""Ground truth, evaluation runs, and reviewer feedback.

These three tables are what keep the system measurable after it leaves the
laptop. `ground_truth` is synthetic-only today; `feedback_events` is the real
thing's replacement for it, and both carry a comparison vector so a future
supervised refit reads from one shape rather than two.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
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

from concordance.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from concordance.db.enums import (
    EvalStrategy,
    ExpectedOutcome,
    FeedbackLabel,
    LabKind,
    RunStatus,
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


class LabSweep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One Lab experiment: a corruption sweep, or an LLM sample that extends one.

    The `eval_runs` rows it produced point back here, so "the robustness curve"
    is always one experiment's cells and never a mixture of two runs made with
    different seeds. An LLM experiment names the sweep it extends in
    `parent_id`: it reuses that sweep's datasets and seed, so its points can be
    drawn on the same axes.
    """

    __tablename__ = "lab_sweeps"

    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("lab_sweeps.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=RunStatus.QUEUED
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="SET NULL")
    )
    #: What was asked for: levels, strategies, seed, dataset size, sample size.
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: `{"done": n, "total": m}` - levels for a sweep, model calls for an LLM run.
    progress: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Wall time, per-level errors, anything the page reports but does not chart.
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(String(2000))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        check_values("kind", LabKind),
        check_values("status", RunStatus),
        Index("ix_lab_sweeps_kind_created_at", "kind", "created_at"),
        Index("ix_lab_sweeps_parent_id", "parent_id"),
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
    sweep_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("lab_sweeps.id", ondelete="CASCADE")
    )
    #: Everything the columns do not hold: per-scenario tallies, routes, the
    #: fit's before/after calibration, an LLM sample's intervals and costs.
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        check_values("strategy", EvalStrategy),
        Index("ix_eval_runs_strategy_corruption_level", "strategy", "corruption_level"),
        Index("ix_eval_runs_sweep_id", "sweep_id"),
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


__all__ = ["EvalRun", "FeedbackEvent", "GroundTruth", "LabSweep"]
