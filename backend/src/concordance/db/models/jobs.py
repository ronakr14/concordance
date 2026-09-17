"""The job queue table.

Dequeuing is `SELECT ... FOR UPDATE SKIP LOCKED` at Stage 6, which is why
`locked_at` and `locked_by` are columns rather than an advisory lock: a worker
that dies holding a lock has to be visible to the next worker, and a row a human
can read is what makes a stuck job diagnosable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from concordance.db.base import Base, TimestampMixin
from concordance.db.enums import JobStatus, check_values


class Job(TimestampMixin, Base):
    """One unit of background work."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=JobStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    #: Not before this instant. Carries both the initial delay and the
    #: exponential backoff between attempts.
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        check_values("status", JobStatus),
        Index("ix_jobs_status_run_after", "status", "run_after"),
        Index("ix_jobs_kind", "kind"),
    )


__all__ = ["Job"]
