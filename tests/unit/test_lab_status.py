"""A Lab experiment whose process died must stop reading as live.

One live experiment at a time is enforced by reading `effective_status`, so a
row stuck in `RUNNING` would block the Lab for good. With a job, the job's own
status settles it (covered against the database in `test_lab.py`); without one
- run inline from the CLI - silence past `STALE_AFTER` does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from concordance.db.models import LabSweep
from concordance.lab.service import STALE_AFTER, effective_status

pytestmark = pytest.mark.unit


def _row(status: str, minutes_since_progress: float) -> LabSweep:
    now = datetime.now(UTC)
    return LabSweep(
        kind="sweep",
        status=status,
        job_id=None,
        updated_at=now - timedelta(minutes=minutes_since_progress),
        error=None,
    )


def test_a_jobless_run_that_is_making_progress_is_live() -> None:
    assert effective_status(None, _row("RUNNING", 5)) == ("RUNNING", None)  # type: ignore[arg-type]


def test_a_jobless_run_silent_past_the_threshold_reads_as_failed() -> None:
    row = _row("RUNNING", STALE_AFTER.total_seconds() / 60 + 1)
    status, error = effective_status(None, row)  # type: ignore[arg-type]
    assert status == "FAILED"
    assert error is not None and "no progress" in error


def test_a_jobless_queued_row_ages_out_too_and_a_finished_one_is_left_alone() -> None:
    # Nothing ever claims a jobless row, so waiting in QUEUED is the same silence.
    assert effective_status(None, _row("QUEUED", 5))[0] == "QUEUED"  # type: ignore[arg-type]
    assert effective_status(None, _row("QUEUED", 600))[0] == "FAILED"  # type: ignore[arg-type]
    assert effective_status(None, _row("COMPLETED", 600))[0] == "COMPLETED"  # type: ignore[arg-type]
