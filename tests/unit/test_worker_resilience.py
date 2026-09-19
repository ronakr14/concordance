"""The worker survives a lost database connection instead of exiting."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from concordance.config import Settings
from concordance.jobs import worker as worker_module
from concordance.jobs.scheduler import IntervalScheduler
from concordance.jobs.worker import Worker

pytestmark = pytest.mark.unit


def _dropped() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))


def test_a_lost_connection_backs_off_and_the_loop_carries_on(monkeypatch: Any) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(worker_module.time, "sleep", sleeps.append)

    worker = Worker(settings=Settings(), name="t", scheduler=IntervalScheduler([]), max_jobs=1)
    monkeypatch.setattr(worker, "_reclaim_stale", lambda: None)
    outcomes: list[Any] = [_dropped(), _dropped(), _dropped(), 42]

    def run_once() -> int | None:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        worker.stats.claimed += 1
        return outcome

    monkeypatch.setattr(worker, "run_once", run_once)
    stats = worker.run()

    assert stats.db_errors == 3 and stats.claimed == 1
    assert sleeps == [1.0, 2.0, 4.0], "doubling backoff between attempts"


def test_the_backoff_is_capped_and_resets_after_a_success(monkeypatch: Any) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(worker_module.time, "sleep", sleeps.append)

    worker = Worker(settings=Settings(), name="t", scheduler=IntervalScheduler([]), max_jobs=2)
    monkeypatch.setattr(worker, "_reclaim_stale", lambda: None)
    outcomes: list[Any] = [*[_dropped()] * 7, 1, _dropped(), 2]

    def run_once() -> int | None:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        worker.stats.claimed += 1
        return outcome

    monkeypatch.setattr(worker, "run_once", run_once)
    worker.run()

    assert sleeps[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert sleeps[7] == 1.0, "a success resets the backoff"
