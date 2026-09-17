"""The queue under the conditions it exists for: two workers, crashes, retries.

Every test here uses a job kind registered only for the suite, so nothing
reconciles anything and the assertions are about the queue rather than about
matching. The marker handler appends an `audit_logs` row per execution, which
is how double execution becomes visible: two workers racing on the same job
produce two rows with the same job id, and the test counts them.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

MARKER_KIND = "test_marker"
SLOW_KIND = "test_slow_marker"
FAILING_KIND = "test_always_fails"


@pytest.fixture(scope="module", autouse=True)
def _register_test_handlers() -> Iterator[None]:
    """Register the suite's handlers once, and take them out again afterwards."""
    from concordance.db.repositories.audit import AuditRepository
    from concordance.jobs import registry

    def marker(session: Any, _settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
        AuditRepository(session).record(
            action="test.marker",
            entity_type="test_job",
            entity_id=str(payload.get("tag", "")),
            actor_role="system",
            after={"worker": payload.get("tag")},
        )
        return {"tag": payload.get("tag")}

    def slow_marker(session: Any, settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
        import time

        time.sleep(float(payload.get("seconds", 0.3)))
        return marker(session, settings, payload)

    def always_fails(_session: Any, _settings: Any, _payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("this handler always fails")

    registry.register(MARKER_KIND)(marker)
    registry.register(SLOW_KIND)(slow_marker)
    registry.register(FAILING_KIND)(always_fails)
    yield
    for kind in (MARKER_KIND, SLOW_KIND, FAILING_KIND):
        registry._HANDLERS.pop(kind, None)


@pytest.fixture
def settings(owner_url: str) -> Any:
    """Settings pointed at the real database, with the engine reset around it."""
    from concordance.config import Settings
    from concordance.db.session import dispose_engine

    dispose_engine()
    resolved = Settings(DATABASE_URL=owner_url, DB_CONNECT_TIMEOUT=20)
    yield resolved
    dispose_engine()


@pytest.fixture
def clean_queue(settings: Any) -> Iterator[str]:
    """A tag unique to this test, and no test jobs left behind afterwards."""
    from sqlalchemy import delete

    from concordance.db.models import AuditLog, Job
    from concordance.db.session import session_scope

    tag = uuid.uuid4().hex[:12]
    yield tag
    with session_scope(settings) as session:
        session.execute(delete(Job).where(Job.kind.in_([MARKER_KIND, SLOW_KIND, FAILING_KIND])))
        session.execute(delete(AuditLog).where(AuditLog.entity_id == tag))


def _worker(settings: Any, name: str, **kwargs: Any) -> Any:
    from concordance.jobs.scheduler import IntervalScheduler
    from concordance.jobs.worker import Worker

    return Worker(
        settings=settings,
        name=name,
        kinds=[MARKER_KIND, SLOW_KIND, FAILING_KIND],
        poll_seconds=0.05,
        # No scheduled work: this suite is about the queue, and a daily job
        # firing inside it would be a second thing to reason about.
        scheduler=IntervalScheduler(entries=[]),
        **kwargs,
    )


def _executions(settings: Any, tag: str) -> int:
    from sqlalchemy import func, select

    from concordance.db.models import AuditLog
    from concordance.db.session import session_scope

    with session_scope(settings) as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_type == "test_job", AuditLog.entity_id == tag)
            )
            or 0
        )


def test_two_workers_share_a_queue_without_double_execution(
    settings: Any, clean_queue: str
) -> None:
    """`SKIP LOCKED` plus claim-then-commit. The reason the queue can scale out."""
    from sqlalchemy import select

    from concordance.db.enums import JobStatus
    from concordance.db.models import Job
    from concordance.db.session import session_scope
    from concordance.jobs.worker import enqueue

    jobs = 8
    for _ in range(jobs):
        enqueue(SLOW_KIND, {"tag": clean_queue, "seconds": 0.15}, settings=settings)

    workers = [_worker(settings, f"w{i}", idle_timeout=0.0) for i in range(2)]
    threads = [threading.Thread(target=w.run) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert _executions(settings, clean_queue) == jobs, "a job ran twice, or not at all"
    assert sum(w.stats.done for w in workers) == jobs
    assert all(w.stats.done > 0 for w in workers), "one worker took everything; not a race test"

    with session_scope(settings) as session:
        statuses = set(
            session.scalars(select(Job.status).where(Job.kind == SLOW_KIND)).all()
        )
    assert statuses == {str(JobStatus.DONE)}


def test_a_stale_lock_is_reclaimed_rather_than_stuck(settings: Any, clean_queue: str) -> None:
    """A worker killed mid-job leaves a row the next worker can take."""
    from concordance.db.enums import JobStatus
    from concordance.db.models import Job
    from concordance.db.repositories.jobs import JobRepository
    from concordance.db.session import session_scope
    from concordance.jobs.worker import enqueue

    job_id = enqueue(MARKER_KIND, {"tag": clean_queue}, settings=settings)

    with session_scope(settings) as session:
        claimed = JobRepository(session).claim("worker-that-dies", kinds=[MARKER_KIND])
        assert claimed is not None
        assert claimed.status == str(JobStatus.RUNNING)
        assert claimed.locked_by == "worker-that-dies"

    # The worker dies here: the lock stays behind, and it is old.
    with session_scope(settings) as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.locked_at = datetime.now(UTC) - timedelta(hours=2)

    with session_scope(settings) as session:
        assert JobRepository(session).claim("worker-two", kinds=[MARKER_KIND]) is None

    with session_scope(settings) as session:
        assert JobRepository(session).reclaim_stale(older_than=timedelta(minutes=30)) >= 1

    with session_scope(settings) as session:
        retaken = JobRepository(session).claim("worker-two", kinds=[MARKER_KIND])
        assert retaken is not None
        assert retaken.id == job_id
        assert retaken.attempts == 2, "the failed attempt is still counted"


def test_a_failing_job_backs_off_and_then_dead_letters(settings: Any, clean_queue: str) -> None:
    """Three states worth distinguishing: retryable, retrying later, and dead."""
    from concordance.db.enums import JobStatus
    from concordance.db.models import Job
    from concordance.db.repositories.jobs import JobRepository
    from concordance.db.session import session_scope
    from concordance.jobs.worker import enqueue

    job_id = enqueue(FAILING_KIND, {"tag": clean_queue}, settings=settings, max_attempts=2)

    worker = _worker(settings, "failing-worker", max_jobs=1, idle_timeout=0.0)
    worker.run()

    with session_scope(settings) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == str(JobStatus.PENDING), "the first failure retries"
        assert job.attempts == 1
        assert "this handler always fails" in (job.last_error or "")
        assert job.run_after > datetime.now(UTC), "backoff pushed it into the future"
        assert job.locked_by is None
        # Make it due again so the second attempt can be observed without
        # waiting out the backoff.
        job.run_after = datetime.now(UTC)

    worker = _worker(settings, "failing-worker-2", max_jobs=1, idle_timeout=0.0)
    worker.run()

    with session_scope(settings) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == str(JobStatus.DEAD), "max_attempts spent"
        assert job.attempts == 2
        assert "this handler always fails" in (job.last_error or "")
        assert JobRepository(session).claim("anyone", kinds=[FAILING_KIND]) is None


def test_an_unregistered_kind_dead_letters_on_the_first_attempt(
    settings: Any, clean_queue: str
) -> None:
    """Retrying a job nothing can run only delays the diagnosis."""
    from concordance.db.enums import JobStatus
    from concordance.db.models import Job
    from concordance.db.session import session_scope
    from concordance.jobs.scheduler import IntervalScheduler
    from concordance.jobs.worker import Worker, enqueue

    job_id = enqueue("test_kind_nothing_handles", {"tag": clean_queue}, settings=settings)
    worker = Worker(
        settings=settings,
        name="unknown-kind-worker",
        kinds=["test_kind_nothing_handles"],
        poll_seconds=0.05,
        scheduler=IntervalScheduler(entries=[]),
        max_jobs=1,
        idle_timeout=0.0,
    )
    worker.run()

    with session_scope(settings) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == str(JobStatus.DEAD)
        assert "no handler registered" in (job.last_error or "")
        session.delete(job)


def test_graceful_shutdown_finishes_the_job_in_hand(settings: Any, clean_queue: str) -> None:
    """SIGTERM stops claiming; it does not abandon work already started."""
    from concordance.db.enums import JobStatus
    from concordance.db.models import Job
    from concordance.db.session import session_scope
    from concordance.jobs.worker import enqueue

    first = enqueue(SLOW_KIND, {"tag": clean_queue, "seconds": 0.2}, settings=settings)
    second = enqueue(MARKER_KIND, {"tag": clean_queue}, settings=settings)

    worker = _worker(settings, "graceful", idle_timeout=0.0)
    stopper = threading.Timer(0.05, worker.stop)
    stopper.start()
    worker.run()
    stopper.cancel()

    with session_scope(settings) as session:
        assert session.get(Job, first).status == str(JobStatus.DONE), "the job in hand finished"
        assert session.get(Job, second).status == str(JobStatus.PENDING), "no new job claimed"
    assert _executions(settings, clean_queue) == 1
