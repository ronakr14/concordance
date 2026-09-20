"""Starting and cancelling runs from outside the worker - what the API calls.

**The run row exists before the work starts.** `start_run` writes the run in
`QUEUED` and a job that names it, in one transaction, and returns the run id at
once. The worker adopts that row rather than creating its own, so the id the
client polls is the id that ends up holding the results. The job is given a
single attempt: a run that failed part-way has already superseded older
results, and a retry under the same id would collide with its own rows.

**One live run per scope**, enforced twice. The check here produces a readable
409 naming the run in the way; the partial unique index on the table is what
makes it true when two requests race past the check together.

**Cancelling is cooperative.** A queued run is cancelled outright and its job
does nothing when claimed. A running one is marked, and the worker stops at the
next chunk boundary - never mid-chunk, so what it leaves behind is whole
chunks, supersedes and conflict flags included.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from concordance.audit import service as audit
from concordance.audit.service import Actor
from concordance.db.enums import EvalStrategy, RunStatus, SanctionFileStatus, values
from concordance.db.models import ReconciliationRun, SanctionFile, SanctionRecord, ScoringConfig
from concordance.db.repositories.jobs import JobRepository
from concordance.db.repositories.matches import MatchRepository
from concordance.errors import ConflictError, InvalidError, NotFoundError

LIVE = (str(RunStatus.QUEUED), str(RunStatus.RUNNING))


def start_run(
    session: Session,
    actor: Actor,
    *,
    strategy: str = str(EvalStrategy.PROBABILISTIC),
    scoring_config_id: uuid.UUID | None = None,
    file_id: uuid.UUID | None = None,
    limit: int | None = None,
    max_candidates: int | None = None,
) -> ReconciliationRun:
    if strategy not in values(EvalStrategy):
        raise InvalidError(
            f"unknown strategy {strategy!r}",
            details={"strategy": f"one of {', '.join(values(EvalStrategy))}"},
        )

    config = _config(session, scoring_config_id)
    if file_id is not None:
        _check_file(session, file_id)

    live = session.scalar(
        select(ReconciliationRun).where(
            ReconciliationRun.status.in_(LIVE),
            ReconciliationRun.file_id.is_(None)
            if file_id is None
            else ReconciliationRun.file_id == file_id,
        )
    )
    if live is not None:
        raise _in_progress(live.id)

    payload: dict[str, Any] = {
        "strategy": strategy,
        "config_version": config.version if config else None,
        "file_id": str(file_id) if file_id else None,
        "limit": limit,
        "max_candidates": max_candidates,
        "triggered_by": str(actor.user_id) if actor.user_id else None,
    }
    run = ReconciliationRun(
        triggered_by=actor.user_id,
        file_id=file_id,
        status=str(RunStatus.QUEUED),
        strategy=strategy,
        scoring_config_id=config.id if config else None,
        request=payload,
    )
    session.add(run)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _in_progress(None) from exc

    job = JobRepository(session).enqueue("reconcile", {**payload, "run_id": str(run.id)}, max_attempts=1)
    session.flush()
    run.job_id = job.id
    audit.record(
        session,
        actor,
        "run.requested",
        entity_type="reconciliation_run",
        entity_id=run.id,
        after={**payload, "job_id": job.id},
    )
    return run


def cancel_run(session: Session, actor: Actor, run_id: uuid.UUID) -> ReconciliationRun:
    # `of=` names the table to lock, and it is not optional here.
    # `ReconciliationRun.scoring_config` is `lazy="joined"`, so loading a run
    # emits a LEFT OUTER JOIN, and Postgres refuses `FOR UPDATE` on the
    # nullable side of one: a bare `with_for_update=True` fails the whole
    # request with `FeatureNotSupported`. Locking the run's own row is also
    # what was meant - the config is read, never written, on this path.
    run = session.get(
        ReconciliationRun, run_id, with_for_update={"of": ReconciliationRun}
    )
    if run is None:
        raise NotFoundError(f"no run {run_id}")
    if run.status not in LIVE:
        raise ConflictError(
            f"run is {run.status}; only a queued or running run can be cancelled",
            code="run_finished",
            details={"status": run.status},
        )
    before = {"status": run.status}
    was_queued = run.status == RunStatus.QUEUED
    run.status = str(RunStatus.CANCELLED)
    if was_queued:
        run.finished_at = datetime.now(UTC)
    audit.record(
        session,
        actor,
        "run.cancelled",
        entity_type="reconciliation_run",
        entity_id=run.id,
        before=before,
        after={"status": run.status, "stops": "immediately" if was_queued else "after the current chunk"},
    )
    return run


def _config(session: Session, config_id: uuid.UUID | None) -> ScoringConfig | None:
    if config_id is None:
        # Pinned now rather than when the worker gets to it, so the run row
        # shows which weights will decide it from the moment it is queued. With
        # no config in the table yet, the worker imports one on first use.
        return MatchRepository(session).latest_config()
    config = session.get(ScoringConfig, config_id)
    if config is None:
        raise InvalidError(
            f"no scoring config {config_id}", details={"scoring_config_id": "not found"}
        )
    return config


def _check_file(session: Session, file_id: uuid.UUID) -> None:
    file = session.get(SanctionFile, file_id)
    if file is None:
        raise InvalidError(f"no uploaded file {file_id}", details={"file_id": "not found"})
    if file.status != SanctionFileStatus.COMMITTED:
        raise ConflictError(
            f"file is {file.status}; commit its mapping before reconciling it",
            code="file_not_committed",
            details={"status": file.status},
        )
    total, replaced = session.execute(
        select(
            func.count(),
            func.count().filter(SanctionRecord.is_current.is_(False)),
        ).where(SanctionRecord.file_id == file_id)
    ).one()
    if not total:
        raise ConflictError(
            "every row in this file matched a record already on file unchanged; "
            "there is nothing new to reconcile",
            code="file_has_no_records",
        )
    if replaced:
        # Scoring an outdated version would supersede the result for the newer
        # one with a decision about data that is no longer current.
        raise ConflictError(
            f"{replaced} of this file's records have been replaced by a newer upload; "
            "reconcile the newer file, or run without a file",
            code="file_outdated",
            details={"replaced": int(replaced), "records": int(total)},
        )


def _in_progress(run_id: uuid.UUID | None) -> ConflictError:
    return ConflictError(
        "a run over these records is already queued or running",
        code="run_in_progress",
        details={"run_id": str(run_id)} if run_id else {},
    )


__all__ = ["LIVE", "cancel_run", "start_run"]
