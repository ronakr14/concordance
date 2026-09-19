"""The random audit of auto-rejects: which results it draws, and how often."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from concordance.domain import Outcome
from concordance.jobs.reconcile import _ResultWriter, audit_draw


@dataclass
class _Result:
    record_id: str
    decision: Outcome
    candidates: tuple[Any, ...] = ("c",)


def _writer(rate: float) -> _ResultWriter:
    return _ResultWriter(session=None, run_id=uuid.UUID(int=7), audit_rate=rate)  # type: ignore[arg-type]


def test_the_draw_is_fixed_by_run_and_record() -> None:
    run = uuid.UUID(int=1)
    assert audit_draw(run, "S1") == audit_draw(run, "S1")
    assert audit_draw(run, "S1") != audit_draw(uuid.UUID(int=2), "S1")
    assert 0.0 <= audit_draw(run, "S1") < 1.0


def test_only_auto_rejects_with_candidates_are_ever_drawn() -> None:
    writer = _writer(1.0)
    assert writer._audited(_Result("a", Outcome.NO_MATCH))
    assert not writer._audited(_Result("b", Outcome.NO_MATCH, candidates=()))
    assert not writer._audited(_Result("c", Outcome.MATCH))
    assert not writer._audited(_Result("d", Outcome.AMBIGUOUS))
    assert not _writer(0.0)._audited(_Result("e", Outcome.NO_MATCH))


def test_the_share_drawn_is_the_rate() -> None:
    writer = _writer(0.05)
    drawn = sum(writer._audited(_Result(f"S{i:05d}", Outcome.NO_MATCH)) for i in range(20_000))
    assert writer.audited == drawn
    assert 0.045 < drawn / 20_000 < 0.055
