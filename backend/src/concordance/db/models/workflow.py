"""Cases and the audit log.

`audit_logs` is append-only and the migration enforces it by revoking `UPDATE`
and `DELETE` from the application role. An audit trail the application can edit
is not an audit trail - it is a log that happens to be believed. The revoke is
the mechanism; a test that attempts an update and expects a permission error is
the proof, because a `REVOKE` that silently applied to the wrong role looks
identical to one that worked.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from concordance.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from concordance.db.enums import CaseStatus, check_values


class Case(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A confirmed match under review for a fixed window.

    The partial unique index is the important constraint: one provider can hold
    at most one `ACTIVE` case per sanction record. Closed and expired cases
    accumulate freely, which is what makes the history readable, and the index
    still refuses a second live one.
    """

    __tablename__ = "cases"

    #: Human-readable, e.g. `CASE-2026-000123`. What a reviewer quotes.
    case_number: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.provider_id", ondelete="CASCADE"), nullable=False
    )
    sanction_record_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sanction_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    match_result_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("match_results.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=CaseStatus.ACTIVE
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    duration_months: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    close_reason: Mapped[str | None] = mapped_column(Text)

    #: Q5. A later run disagreed with the result this case was opened on. The
    #: case is not closed automatically - a human decides - but it is surfaced.
    conflict_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    conflict_match_result_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("match_results.id", ondelete="SET NULL")
    )

    __table_args__ = (
        check_values("status", CaseStatus),
        Index("uq_cases_case_number", "case_number", unique=True),
        Index(
            "uq_cases_active_provider_sanction",
            "provider_id",
            "sanction_record_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_cases_status_end_date", "status", "end_date"),
        Index("ix_cases_conflict_flag", "conflict_flag", postgresql_where=text("conflict_flag")),
    )


class AuditLog(CreatedAtMixin, Base):
    """Append-only record of who did what to which entity.

    `actor_role` is denormalized on purpose. The audit row has to stay true
    after the user's role changes or the user is deleted, and a join to `users`
    would report today's role for yesterday's action.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_role: Mapped[str | None] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(INET)

    __table_args__ = (
        Index("ix_audit_logs_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_audit_logs_created_at", text("created_at DESC")),
        Index("ix_audit_logs_actor_user_id", "actor_user_id"),
    )


__all__ = ["AuditLog", "Case"]
