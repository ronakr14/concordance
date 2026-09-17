"""The parts of Stage 6 that need no database: isolation, scheduling, routing.

The three behaviours asserted here are the ones that are hard to notice when
they break and expensive when they do - a run that dies on one bad record, a
scheduler that fires twice, a job kind nothing handles.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from concordance.domain import Outcome
from concordance.jobs.registry import (
    UnknownJobKindError,
    handler_for,
    is_known,
    known_kinds,
    register,
)
from concordance.jobs.scheduler import IntervalScheduler, ScheduledJob, default_schedule
from concordance.matching.comparators import ModelKind
from concordance.matching.engine import ENGINE_VERSION, ReconciliationEngine, RunCounters
from concordance.matching.scorer import DecisionReason, MatchResult, Route
from concordance.matching.strategies import StrategyName

pytestmark = pytest.mark.unit


class _Recorded:
    """A strategy that answers from a script, and raises where the script says so."""

    name = StrategyName.PROBABILISTIC

    def __init__(self, answers: dict[str, Outcome | Exception]) -> None:
        self.answers = answers
        self.seen: list[str] = []

    def decide(self, record, _candidates):  # type: ignore[no-untyped-def]
        self.seen.append(record.record_id)
        answer = self.answers[record.record_id]
        if isinstance(answer, Exception):
            raise answer
        return MatchResult(
            record_id=record.record_id,
            decision=answer,
            route=Route.PROBABILISTIC,
            reason=DecisionReason.ABOVE_ACCEPT,
            kind=ModelKind.INDIVIDUAL,
        )

    def stats(self) -> dict[str, object]:
        return {"strategy": str(self.name), "adjudicated": 0, "tokens": 0, "cost_usd": 0.0}


class _Record:
    def __init__(self, record_id: str) -> None:
        self.record_id = record_id


def _work(ids: list[str]) -> list[tuple[str, object, list]]:
    return [(rid, _Record(rid), []) for rid in ids]


def test_one_bad_record_fails_that_record_only() -> None:
    """The whole point of running five thousand of these in a batch."""
    strategy = _Recorded(
        {
            "R1": Outcome.MATCH,
            "R2": ValueError("comparator blew up"),
            "R3": Outcome.NO_MATCH,
        }
    )
    engine = ReconciliationEngine(strategy=strategy)

    outcomes = list(engine.decide_many(_work(["R1", "R2", "R3"])))

    assert [o.record_id for o in outcomes] == ["R1", "R2", "R3"]
    assert strategy.seen == ["R1", "R2", "R3"], "the run continued past the failure"
    failed = outcomes[1]
    assert failed.failed
    assert failed.result is None
    assert failed.error == "ValueError: comparator blew up"
    assert engine.counters.records_total == 3
    assert engine.counters.failed == 1
    assert engine.counters.matched == 1
    assert engine.counters.no_match == 1


def test_counters_split_the_three_outcomes() -> None:
    counters = RunCounters()
    engine = ReconciliationEngine(
        strategy=_Recorded(
            {"A": Outcome.MATCH, "B": Outcome.AMBIGUOUS, "C": Outcome.AMBIGUOUS}
        ),
        counters=counters,
    )

    list(engine.decide_many(_work(["A", "B", "C"])))

    assert counters.as_dict()["matched"] == 1
    assert counters.as_dict()["ambiguous"] == 2
    assert counters.as_dict()["no_match"] == 0
    assert counters.stage_seconds["score"] >= 0.0


def test_engine_version_is_reported_on_every_run() -> None:
    """Provenance: a decision nobody can attribute to a version is not replayable."""
    engine = ReconciliationEngine(strategy=_Recorded({}))
    assert engine.stats()["engine_version"] == ENGINE_VERSION
    assert engine.engine_version == ENGINE_VERSION


def test_llm_totals_come_from_the_adjudicator() -> None:
    counters = RunCounters()
    counters.absorb_adjudicator({"adjudicated": 12, "tokens": 3400, "cost_usd": 0.0021})
    assert counters.llm_calls == 12
    assert counters.llm_tokens == 3400
    assert counters.as_dict()["llm_cost_usd"] == 0.0021


# --------------------------------------------------------------------------
# the scheduler
# --------------------------------------------------------------------------


def test_startup_catch_up_fires_once_then_waits_for_the_interval() -> None:
    """A worker that was down over the weekend still runs the missed job."""
    scheduler = IntervalScheduler(
        entries=[ScheduledJob(kind="expire_cases", interval=timedelta(days=1))]
    )
    start = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)

    assert [e.kind for e in scheduler.due(start)] == ["expire_cases"]
    scheduler.mark("expire_cases", start)

    assert scheduler.due(start + timedelta(hours=23)) == []
    assert [e.kind for e in scheduler.due(start + timedelta(days=1))] == ["expire_cases"]


def test_an_entry_without_catch_up_waits_for_its_first_interval() -> None:
    scheduler = IntervalScheduler(
        entries=[ScheduledJob(kind="sweep", interval=timedelta(hours=6), catch_up=False)]
    )
    start = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)

    assert scheduler.due(start) == []
    assert [e.kind for e in scheduler.due(start + timedelta(minutes=1))] == ["sweep"]


def test_the_default_schedule_is_case_expiry_daily() -> None:
    scheduler = default_schedule()
    assert [e.kind for e in scheduler.entries] == ["expire_cases"]
    assert scheduler.entries[0].interval == timedelta(days=1)
    assert scheduler.entries[0].catch_up is True


# --------------------------------------------------------------------------
# the handler registry
# --------------------------------------------------------------------------


def test_every_kind_the_plan_names_has_a_handler() -> None:
    import concordance.jobs.handlers  # noqa: F401  - registers them

    for kind in ("reconcile", "eval", "sweep", "retune", "expire_cases"):
        assert is_known(kind), f"{kind} has no handler"
        assert callable(handler_for(kind))
    assert "reconcile" in known_kinds()


def test_an_unknown_kind_raises_rather_than_returning_none() -> None:
    with pytest.raises(UnknownJobKindError):
        handler_for("no_such_kind")


def test_registering_the_same_kind_twice_is_refused() -> None:
    """Two handlers for one kind means the second silently never runs."""

    @register("test_only_kind")
    def _first(_session, _settings, _payload):  # type: ignore[no-untyped-def]
        return {}

    with pytest.raises(ValueError, match="already registered"):

        @register("test_only_kind")
        def _second(_session, _settings, _payload):  # type: ignore[no-untyped-def]
            return {}
