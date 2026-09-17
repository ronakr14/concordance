"""The job queue.

`claim()` is the interesting method and Stage 6 builds the worker loop on it.
`SELECT ... FOR UPDATE SKIP LOCKED` is what lets two workers pull from the same
queue without coordinating: each takes a row the other has not locked, and
neither blocks. Without `SKIP LOCKED` the second worker waits on the first's
row lock and the queue serialises.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from concordance.db.enums import JobStatus
from concordance.db.models import Job
from concordance.db.repositories.base import Page, paginate

#: A `RUNNING` job whose lock is older than this is assumed to belong to a dead
#: worker and may be reclaimed. Long enough that a slow reconciliation is not
#: stolen from a worker that is still alive.
STALE_LOCK = timedelta(minutes=30)


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        run_after: datetime | None = None,
        max_attempts: int = 3,
    ) -> Job:
        job = Job(
            kind=kind,
            payload=payload or {},
            max_attempts=max_attempts,
            run_after=run_after or datetime.now(UTC),
        )
        self.session.add(job)
        return job

    def claim(self, worker: str, *, kinds: list[str] | None = None) -> Job | None:
        """Take the next due job, or `None`. Locks the row for this transaction."""
        now = datetime.now(UTC)
        stmt = (
            select(Job)
            .where(Job.status == JobStatus.PENDING, Job.run_after <= now)
            .order_by(Job.run_after, Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if kinds:
            stmt = stmt.where(Job.kind.in_(kinds))
        job = self.session.scalar(stmt)
        if job is None:
            return None
        job.status = str(JobStatus.RUNNING)
        job.locked_at = now
        job.locked_by = worker
        job.attempts += 1
        return job

    def reclaim_stale(self, *, older_than: timedelta = STALE_LOCK) -> int:
        """Return jobs whose worker died to the pending pool. Returns the count."""
        cutoff = datetime.now(UTC) - older_than
        # `rowcount` is a CursorResult attribute; an UPDATE always produces one,
        # and the cast is how the type checker is told so.
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(Job)
                .where(Job.status == JobStatus.RUNNING, Job.locked_at < cutoff)
                .values(status=str(JobStatus.PENDING), locked_at=None, locked_by=None)
            ),
        )
        return int(result.rowcount or 0)

    def finish(self, job: Job) -> None:
        job.status = str(JobStatus.DONE)
        job.locked_at = None
        job.locked_by = None
        job.last_error = None

    def fail(self, job: Job, error: str, *, backoff_seconds: float = 30.0) -> None:
        """Retry with backoff, or dead-letter once `max_attempts` is spent.

        The error text is kept on the dead row. A dead-letter queue whose rows
        do not say why they died is a list of mysteries.
        """
        job.last_error = error[:2000]
        job.locked_at = None
        job.locked_by = None
        if job.attempts >= job.max_attempts:
            job.status = str(JobStatus.DEAD)
            return
        job.status = str(JobStatus.PENDING)
        job.run_after = datetime.now(UTC) + timedelta(
            seconds=backoff_seconds * (2 ** (job.attempts - 1))
        )

    def has_pending(self, kind: str) -> bool:
        """Whether a job of this kind is already waiting.

        The scheduler asks before enqueuing, which is how two workers proposing
        the same daily job produce one row rather than two.
        """
        return (
            self.session.scalar(
                select(Job.id).where(Job.kind == kind, Job.status == JobStatus.PENDING).limit(1)
            )
            is not None
        )

    def list_jobs(
        self, *, status: str | None = None, limit: int | None = None, offset: int = 0
    ) -> Page[Job]:
        stmt = select(Job).order_by(Job.created_at.desc())
        if status:
            stmt = stmt.where(Job.status == status)
        return paginate(self.session, stmt, limit, offset)


__all__ = ["STALE_LOCK", "JobRepository"]
