"""The worker loop: claim, run, finish or fail, and know when to stop.

The queue is Postgres and the claim is `SELECT ... FOR UPDATE SKIP LOCKED`, so
scaling out is starting a second process. What makes that safe is not the lock
alone but where the transaction boundaries fall, and there are three of them on
purpose:

1. **Claim, then commit.** The row is marked `RUNNING` with this worker's name
   and the attempt counted, and that is committed before any work begins. A
   worker killed mid-job therefore leaves a visible `RUNNING` row with a stale
   lock rather than a row that silently reverts to `PENDING` with its attempt
   uncounted - which is the difference between a crash loop that is diagnosable
   and one that is invisible.
2. **Run the handler in its own transaction.** Everything a handler writes
   commits together or not at all - unless the handler commits deliberately, as
   `reconcile` does per chunk so that a long run is watchable and a failure
   keeps the decisions it already made.
3. **Finish or fail in a third.** Recording the outcome must not be able to
   fail with the work, or a dead job would look pending forever.

Stale locks are reclaimed by whichever worker notices first, which is what
makes a killed worker's job recoverable without an operator. The same path
covers a lost connection: the loop backs off and carries on rather than exit,
and a job whose outcome could not be recorded is left `RUNNING` until its lock
goes stale and it is claimed again. Graceful shutdown
is the mirror image: on SIGTERM the loop stops claiming but finishes the job in
hand, so a rolling restart never abandons work mid-record.
"""

from __future__ import annotations

import os
import signal
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import OperationalError

from concordance.config import Settings, get_settings
from concordance.db.enums import JobStatus
from concordance.db.models import Job
from concordance.db.repositories.jobs import STALE_LOCK, JobRepository
from concordance.db.session import session_scope
from concordance.jobs import handlers as _handlers  # noqa: F401  - registers them
from concordance.jobs.registry import UnknownJobKindError, handler_for, is_known
from concordance.jobs.scheduler import IntervalScheduler, default_schedule
from concordance.logging_setup import get_logger, new_correlation_id

log = get_logger("jobs.worker")

#: Seconds between polls when the queue is empty. Short enough that a run
#: triggered from the UI starts promptly, long enough that an idle worker is
#: not a load generator against a hosted database.
DEFAULT_POLL_SECONDS = 2.0

#: How often to look for jobs abandoned by a dead worker.
DEFAULT_RECLAIM_SECONDS = 60.0

#: Backoff after a lost database connection: doubles from the first value per
#: consecutive failure, capped at the second. A hosted database drops idle
#: connections and blips on DNS; neither should take the worker down.
DB_RETRY_SECONDS = (1.0, 30.0)


def default_worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass
class WorkerStats:
    claimed: int = 0
    done: int = 0
    failed: int = 0
    dead: int = 0
    reclaimed: int = 0
    scheduled: int = 0
    idle_polls: int = 0
    db_errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "done": self.done,
            "failed": self.failed,
            "dead": self.dead,
            "reclaimed": self.reclaimed,
            "scheduled": self.scheduled,
            "idle_polls": self.idle_polls,
            "db_errors": self.db_errors,
        }


@dataclass
class Worker:
    """One process pulling from the queue."""

    settings: Settings = field(default_factory=get_settings)
    name: str = field(default_factory=default_worker_name)
    kinds: list[str] | None = None
    poll_seconds: float = DEFAULT_POLL_SECONDS
    reclaim_seconds: float = DEFAULT_RECLAIM_SECONDS
    stale_after: timedelta = STALE_LOCK
    scheduler: IntervalScheduler = field(default_factory=default_schedule)
    #: Stop after this many jobs. `None` runs until signalled; the tests and
    #: `concordance worker --once` use a number.
    max_jobs: int | None = None
    #: Stop after this long with an empty queue. `None` waits forever.
    idle_timeout: float | None = None
    stats: WorkerStats = field(default_factory=WorkerStats)
    _stopping: bool = False
    _last_reclaim: float = 0.0

    # -- lifecycle --------------------------------------------------------
    def stop(self) -> None:
        """Finish the job in hand, then exit. Safe to call from a signal handler."""
        self._stopping = True

    def install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: Any) -> None:
            log.info("worker.signal", worker=self.name, signal=signum)
            self.stop()

        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):
                # Not the main thread, or the platform will not have it. The
                # loop still stops on `max_jobs` and `idle_timeout`.
                log.debug("worker.signal.unavailable", signal=name)

    # -- the loop ---------------------------------------------------------
    def run(self) -> WorkerStats:
        log.info(
            "worker.start",
            worker=self.name,
            kinds=self.kinds or "all",
            max_jobs=self.max_jobs,
            schedule=[e.kind for e in self.scheduler.entries],
        )
        idle_since: float | None = None
        failures = 0
        while not self._stopping:
            try:
                self._reclaim_stale()
                self._enqueue_scheduled()
                job = self.run_once()
            except OperationalError as exc:
                failures += 1
                self.stats.db_errors += 1
                first, cap = DB_RETRY_SECONDS
                delay = min(cap, first * 2 ** (failures - 1))
                log.warning(
                    "worker.db_unavailable",
                    worker=self.name,
                    attempt=failures,
                    retry_in=delay,
                    error=str(exc.orig or exc).splitlines()[0],
                )
                time.sleep(delay)
                continue
            failures = 0

            if job is not None:
                idle_since = None
                if self.max_jobs is not None and self.stats.claimed >= self.max_jobs:
                    break
                continue

            self.stats.idle_polls += 1
            now = time.monotonic()
            idle_since = now if idle_since is None else idle_since
            if self.idle_timeout is not None and now - idle_since >= self.idle_timeout:
                log.info("worker.idle_timeout", worker=self.name, seconds=self.idle_timeout)
                break
            time.sleep(self.poll_seconds)

        log.info("worker.stop", worker=self.name, **self.stats.as_dict())
        return self.stats

    def run_once(self) -> uuid.UUID | int | None:
        """Claim and execute one job. Returns its id, or `None` if the queue was empty."""
        claimed = self._claim()
        if claimed is None:
            return None
        job_id, kind, payload = claimed
        self._execute(job_id, kind, payload)
        return job_id

    # -- steps ------------------------------------------------------------
    def _claim(self) -> tuple[int, str, dict[str, Any]] | None:
        with session_scope(self.settings) as session:
            job = JobRepository(session).claim(self.name, kinds=self.kinds)
            if job is None:
                return None
            self.stats.claimed += 1
            log.info(
                "worker.claimed",
                worker=self.name,
                job_id=job.id,
                kind=job.kind,
                attempt=job.attempts,
            )
            return job.id, job.kind, dict(job.payload or {})

    def _execute(self, job_id: int, kind: str, payload: dict[str, Any]) -> None:
        new_correlation_id()
        if not is_known(kind):
            # Nothing will ever run this. Burn the attempts now rather than
            # rediscovering it three backoffs later.
            self._finish(job_id, error=f"no handler registered for job kind {kind!r}", fatal=True)
            return

        started = time.perf_counter()
        try:
            with session_scope(self.settings) as session:
                summary = handler_for(kind)(session, self.settings, payload)
        except UnknownJobKindError as exc:
            self._finish(job_id, error=str(exc), fatal=True)
            return
        except Exception as exc:
            log.error(
                "worker.job.failed",
                worker=self.name,
                job_id=job_id,
                kind=kind,
                error=f"{type(exc).__name__}: {exc}",
                seconds=round(time.perf_counter() - started, 2),
            )
            self._finish(job_id, error=f"{type(exc).__name__}: {exc}")
            return

        log.info(
            "worker.job.done",
            worker=self.name,
            job_id=job_id,
            kind=kind,
            seconds=round(time.perf_counter() - started, 2),
            # Namespaced: a handler's summary can carry keys of its own called
            # `seconds` or `kind`, and a clash is a TypeError here - after the
            # work committed and before the job is marked done, which left
            # finished runs looking stuck in RUNNING.
            **{
                f"result_{k}": v
                for k, v in (summary or {}).items()
                if not isinstance(v, (dict, list))
            },
        )
        self._finish(job_id)

    def _finish(self, job_id: int, *, error: str | None = None, fatal: bool = False) -> None:
        """Record the outcome in its own transaction, so it cannot fail with the work."""
        with session_scope(self.settings) as session:
            repo = JobRepository(session)
            job = session.get(Job, job_id)
            if job is None:
                log.warning("worker.job.vanished", worker=self.name, job_id=job_id)
                return
            if error is None:
                repo.finish(job)
                self.stats.done += 1
                return
            if fatal:
                # Retrying an unroutable job cannot help, so it dead-letters on
                # the first attempt with the reason on the row.
                job.attempts = job.max_attempts
            repo.fail(job, error)
            self.stats.failed += 1
            if job.status == str(JobStatus.DEAD):
                self.stats.dead += 1
                log.error("worker.job.dead", worker=self.name, job_id=job_id, error=error)

    def _reclaim_stale(self) -> None:
        now = time.monotonic()
        if now - self._last_reclaim < self.reclaim_seconds:
            return
        self._last_reclaim = now
        with session_scope(self.settings) as session:
            count = JobRepository(session).reclaim_stale(older_than=self.stale_after)
        if count:
            self.stats.reclaimed += count
            log.warning("worker.reclaimed", worker=self.name, jobs=count)

    def _enqueue_scheduled(self) -> None:
        """Enqueue what the in-house scheduler says is due, without duplicating."""
        due = self.scheduler.due()
        if not due:
            return
        with session_scope(self.settings) as session:
            repo = JobRepository(session)
            for entry in due:
                if repo.has_pending(entry.kind):
                    # Another worker already queued this tick. The scheduler
                    # still marks it, so this worker stops proposing it.
                    self.scheduler.mark(entry.kind)
                    continue
                repo.enqueue(entry.kind, dict(entry.payload))
                self.scheduler.mark(entry.kind)
                self.stats.scheduled += 1
                log.info("worker.scheduled", worker=self.name, kind=entry.kind)


def enqueue(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> int:
    """Put a job on the queue from outside a worker. Returns its id."""
    resolved = settings or get_settings()
    with session_scope(resolved) as session:
        job = JobRepository(session).enqueue(
            kind,
            payload or {},
            run_after=run_after or datetime.now(UTC),
            max_attempts=max_attempts,
        )
        session.flush()
        job_id = job.id
    log.info("jobs.enqueued", kind=kind, job_id=job_id)
    return job_id


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_RECLAIM_SECONDS",
    "Worker",
    "WorkerStats",
    "default_worker_name",
    "enqueue",
]
