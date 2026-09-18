"""Runs, fitted configs, results, candidates and LLM calls.

`match_results` is the table the whole application reads from, so two of its
columns carry decisions worth spelling out.

**`superseded_by` rather than an update (Q5).** Re-reconciling a record does not
overwrite its previous result; it writes a new row and points the old one at it.
Active cases therefore keep referring to the evidence they were opened on, and
"what did we decide in March" stays answerable. Every list query filters
`superseded_by IS NULL`, which is what the partial index serves.

**`llm_call_id` rather than an embedded response.** The adjudicator's answer is
cached and reusable; the result is not. Keeping the call in `llm_calls` means a
second record that hits the same cache key shares the row rather than storing a
second copy of the same response.
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
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from concordance.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from concordance.db.enums import (
    Decision,
    EvalStrategy,
    FittedFrom,
    ReviewStatus,
    Route,
    RunStatus,
    check_values,
)


class ScoringConfig(UUIDPrimaryKeyMixin, Base):
    """A fitted Fellegi-Sunter configuration. Immutable once written.

    Immutable because a run records which config decided it. Editing the weights
    under a finished run would silently rewrite history: the run would claim a
    provenance it no longer has, and replay would produce different numbers from
    the same inputs. A change means a new row and a new version.
    """

    __tablename__ = "scoring_configs"

    version: Mapped[str] = mapped_column(String(100), nullable=False)
    #: The m/u probabilities, the lambda prior and the agreement-level tables,
    #: for both models - exactly `ScoringConfig.as_dict()` from the engine.
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    t_auto_accept: Mapped[float] = mapped_column(Float, nullable=False)
    t_auto_reject: Mapped[float] = mapped_column(Float, nullable=False)
    #: The isotonic calibrator's knots, so a stored confidence can be
    #: reproduced without refitting.
    calibrator: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    fitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    fitted_from: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=FittedFrom.EM
    )
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        check_values("fitted_from", FittedFrom),
        Index("uq_scoring_configs_version", "version", unique=True),
    )


class ReconciliationRun(UUIDPrimaryKeyMixin, Base):
    """One reconciliation of one file against the provider master.

    The snapshot hashes are what make a run replayable: they pin the exact
    contents of both sides, so a replay that produces different numbers has
    either different inputs or a changed engine, and the row says which.
    """

    __tablename__ = "reconciliation_runs"

    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("sanction_files.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=RunStatus.QUEUED)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    engine_version: Mapped[str | None] = mapped_column(String(50))
    scoring_config_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("scoring_configs.id", ondelete="SET NULL")
    )
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    #: Which strategy decided this run, and the full request it was started
    #: with. Replay needs both: a run that does not record `max_candidates`
    #: cannot be re-blocked identically, and one that does not record its
    #: strategy can only be replayed by guessing from the routes it produced.
    strategy: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=EvalStrategy.PROBABILISTIC
    )
    request: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    provider_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    sanction_snapshot_hash: Mapped[str | None] = mapped_column(String(64))

    records_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ambiguous_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    no_match_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    llm_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: Numeric, not float: a cost is money and is summed across runs for the
    #: cost panel, where binary rounding error accumulates visibly.
    llm_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    #: The queue row that executes this run, when it was started through the
    #: API. A run started from the CLI has none.
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        check_values("status", RunStatus),
        check_values("strategy", EvalStrategy, name="run_strategy_valid"),
        Index("ix_reconciliation_runs_status", "status"),
        Index("ix_reconciliation_runs_file_id", "file_id"),
        Index("ix_reconciliation_runs_created_at", "created_at"),
        # One live run per scope. Two concurrent runs over the same file would
        # each supersede the other's results in whatever order their chunks
        # landed, and "current" would mean "whichever wrote last". A run with
        # no file is the global scope, hence the coalesce: NULLs never collide
        # in a unique index.
        Index(
            "uq_reconciliation_runs_live_scope",
            text("coalesce(file_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
    )


class LlmCall(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One adjudication request and its response. Also the Stage 5 cache.

    `PostgresCache` reads and writes this table, so a cached hit and an audit
    record are the same row rather than two stores that can disagree about what
    the model said.
    """

    __tablename__ = "llm_calls"

    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")

    __table_args__ = (Index("uq_llm_calls_cache_key", "cache_key", unique=True),)


class MatchResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The engine's decision about one sanction record in one run."""

    __tablename__ = "match_results"

    run_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("reconciliation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sanction_record_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sanction_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    chosen_provider_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("providers.provider_id", ondelete="SET NULL")
    )
    posterior: Mapped[float | None] = mapped_column(Float)
    calibrated_confidence: Mapped[float | None] = mapped_column(Float)
    raw_match_weight: Mapped[float | None] = mapped_column(Float)
    route: Mapped[str] = mapped_column(String(20), nullable=False)
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("llm_calls.id", ondelete="SET NULL")
    )
    #: The engine's reason code and notes, plus the adjudicator's reasoning and
    #: cited evidence when the LLM decided it. Rendered by the Investigation UI.
    explanation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=ReviewStatus.PENDING
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_comment: Mapped[str | None] = mapped_column(Text)
    #: The provider a reviewer confirmed. Separate from `chosen_provider_id`,
    #: which is what the engine said: on an ambiguous result that is only its
    #: top-ranked candidate, the reviewer often picks another, and overwriting
    #: the engine's answer would erase the evidence that they differed.
    approved_provider_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("providers.provider_id", ondelete="SET NULL")
    )

    #: Q5. Set when a later run re-decides the same record; the old row stays
    #: exactly as it was.
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("match_results.id", ondelete="SET NULL")
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        check_values("decision", Decision),
        check_values("route", Route),
        check_values("review_status", ReviewStatus),
        Index(
            "uq_match_results_run_id_sanction_record_id",
            "run_id",
            "sanction_record_id",
            unique=True,
        ),
        Index("ix_match_results_run_id_review_status", "run_id", "review_status"),
        Index("ix_match_results_sanction_record_id", "sanction_record_id"),
        Index(
            "ix_match_results_current",
            "run_id",
            "review_status",
            postgresql_where=text("superseded_by IS NULL"),
        ),
        # The review queue and the KPI tiles read current results by decision
        # and status, and the confidence histogram by confidence. Partial, like
        # the index above, because superseded rows are history and nothing on
        # a dashboard counts them.
        Index(
            "ix_match_results_current_decision",
            "decision",
            "review_status",
            postgresql_where=text("superseded_by IS NULL"),
        ),
        # Descending with nulls last, because that is the queue's default sort:
        # most confident first, and a result with no confidence at all (no
        # candidates) at the bottom. An ascending sort walks it backwards.
        Index(
            "ix_match_results_current_confidence",
            text("calibrated_confidence DESC NULLS LAST"),
            postgresql_where=text("superseded_by IS NULL"),
        ),
        Index("ix_match_results_created_at", "created_at"),
    )


class MatchCandidate(Base):
    """One scored candidate beneath a result, in rank order.

    `field_weights` is the per-field contribution to the total match weight. The
    Investigation UI renders it directly as the evidence table, which is why it
    is stored rather than recomputed: an explanation shown to a reviewer has to
    be the arithmetic that actually produced the decision, not arithmetic redone
    later against a config that may since have been refitted.
    """

    __tablename__ = "match_candidates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_result_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("match_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.provider_id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    field_levels: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    field_weights: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    match_weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    posterior: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    blocking_keys: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    __table_args__ = (
        Index("ix_match_candidates_match_result_id_rank", "match_result_id", "rank"),
        Index("ix_match_candidates_provider_id", "provider_id"),
    )


__all__ = [
    "LlmCall",
    "MatchCandidate",
    "MatchResult",
    "ReconciliationRun",
    "ScoringConfig",
]
