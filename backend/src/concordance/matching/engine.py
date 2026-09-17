"""The full pipeline, versioned, with nothing in it that knows about storage.

The stage before this one hands a record and its candidates to a strategy and
reads back a decision. That is one record. A *run* is five thousand of them,
and the difference is entirely in what happens when one of them misbehaves:

- **One bad record fails that record, not the run.** A record whose
  normalization produced something a comparator cannot handle used to take the
  whole reconciliation down with it. Here it becomes a `RecordOutcome` with an
  `error` and the run continues, which is the only behaviour that makes an
  overnight job worth queuing.
- **`ENGINE_VERSION` is recorded on every run.** A decision is only replayable
  if the thing that decided it can be named. Together with the scoring config
  id, the prompt version and the two snapshot hashes, it is the whole
  provenance of a number - and a replay that disagrees can say which of the
  five changed.
- **Counts and timings are accumulated as the run goes**, not recomputed at the
  end from rows in a table, so a run that dies half way still reports what it
  managed.

The module imports nothing from `db/` and nothing that imports pandas: the
engine is handed prepared work and returns decisions, and whether those arrived
from Parquet or Postgres is not its business. `jobs/reconcile.py` is the half
that knows about both.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from concordance.domain import Outcome
from concordance.matching.normalization import NormalizedRecord
from concordance.matching.scorer import MatchResult
from concordance.matching.strategies import CandidatePair, Strategy

#: Bumped whenever a change could move a decision. Every run row carries it, so
#: "the engine changed" is a fact on the row rather than an inference from a
#: commit date.
ENGINE_VERSION = "1.0.0"

#: One unit of work: the record id, its normalized form, and its candidates.
PreparedRecord = tuple[str, NormalizedRecord, Sequence[CandidatePair]]


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    """What the engine made of one record. Exactly one of `result`/`error` is set."""

    record_id: str
    result: MatchResult | None = None
    error: str | None = None
    elapsed_ms: float = 0.0

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass
class RunCounters:
    """Everything a `reconciliation_runs` row reports, accumulated live."""

    records_total: int = 0
    matched: int = 0
    ambiguous: int = 0
    no_match: int = 0
    failed: int = 0
    llm_calls: int = 0
    llm_tokens: int = 0
    llm_cost_usd: float = 0.0
    stage_seconds: dict[str, float] = field(default_factory=dict)

    def observe(self, outcome: RecordOutcome) -> None:
        self.records_total += 1
        if outcome.failed or outcome.result is None:
            self.failed += 1
            return
        decision = outcome.result.decision
        if decision is Outcome.MATCH:
            self.matched += 1
        elif decision is Outcome.AMBIGUOUS:
            self.ambiguous += 1
        else:
            self.no_match += 1

    def add_stage(self, name: str, seconds: float) -> None:
        self.stage_seconds[name] = self.stage_seconds.get(name, 0.0) + seconds

    def absorb_adjudicator(self, stats: dict[str, Any]) -> None:
        """Take the LLM totals from whichever adjudicator ran.

        Read rather than counted per record because an adjudicator that serves
        a cached answer is not a call, and only the adjudicator knows which of
        its answers cost anything.
        """
        self.llm_calls = int(stats.get("adjudicated") or 0)
        self.llm_tokens = int(stats.get("tokens") or 0)
        self.llm_cost_usd = float(stats.get("cost_usd") or 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "records_total": self.records_total,
            "matched": self.matched,
            "ambiguous": self.ambiguous,
            "no_match": self.no_match,
            "failed": self.failed,
            "llm_calls": self.llm_calls,
            "llm_tokens": self.llm_tokens,
            "llm_cost_usd": round(self.llm_cost_usd, 6),
            "stage_seconds": {k: round(v, 4) for k, v in self.stage_seconds.items()},
        }


@dataclass
class ReconciliationEngine:
    """A strategy, a version, and the bookkeeping a run needs around it."""

    strategy: Strategy
    engine_version: str = ENGINE_VERSION
    counters: RunCounters = field(default_factory=RunCounters)

    @property
    def strategy_name(self) -> str:
        return str(self.strategy.name)

    def decide(self, record_id: str, normalized: NormalizedRecord, candidates: Sequence[CandidatePair]) -> RecordOutcome:
        """One record. Never raises: a failure is a value, not an exception."""
        started = time.perf_counter()
        try:
            result = self.strategy.decide(normalized, candidates)
        # A record the comparators cannot handle fails that record only.
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000.0
            return RecordOutcome(
                record_id=record_id,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=elapsed,
            )
        return RecordOutcome(
            record_id=record_id,
            result=result,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    def decide_many(self, work: Iterable[PreparedRecord]) -> Iterator[RecordOutcome]:
        """Score a batch, counting as it goes. Lazy, so the caller can persist per chunk."""
        for record_id, normalized, candidates in work:
            started = time.perf_counter()
            outcome = self.decide(record_id, normalized, candidates)
            self.counters.add_stage("score", time.perf_counter() - started)
            self.counters.observe(outcome)
            yield outcome

    def stats(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "strategy": self.strategy_name,
            **self.counters.as_dict(),
            "adjudicator": self.strategy.stats(),
        }


__all__ = [
    "ENGINE_VERSION",
    "PreparedRecord",
    "ReconciliationEngine",
    "RecordOutcome",
    "RunCounters",
]
