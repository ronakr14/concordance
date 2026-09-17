"""Replay: re-execute a finished run and prove it reaches the same decisions.

This is the audit time-machine, and its value is entirely in what it refuses to
do. A replay that quietly scored against today's data, today's weights, or a
fresh model call would always agree with itself and prove nothing. So:

**It refuses when the inputs have moved.** Both snapshot hashes are recomputed
and compared with the ones the run recorded. A provider row edited since the run
means the question "would we decide this the same way" can no longer be asked of
this run, and saying so is the honest answer. `--force` exists for the case
where a human wants to see the divergence anyway, and the report says plainly
that the hashes did not match.

**It uses the stored versions, not the current ones.** The scoring config comes
from the row the run points at, the strategy and blocking cap come from the
request the run recorded, and the engine version is compared rather than
assumed.

**It never calls a model.** The router runs offline: a cached answer is served,
a cache miss is an error, and the report's `llm_calls: 0` is therefore a fact
rather than a hope. This is why `llm_calls` is a cache table rather than a log.

**It writes nothing.** A replay is a question about history, and a replay that
inserted its own `match_results` would supersede the very rows it is checking.
The comparison happens in memory and the report is the output.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from concordance.config import Settings
from concordance.db.models import MatchResult, ReconciliationRun
from concordance.db.models import SanctionRecord as SanctionRecordRow
from concordance.db.models import ScoringConfig as ScoringConfigRow
from concordance.jobs.reconcile import RunRequest, build_adjudicator_for
from concordance.logging_setup import get_logger
from concordance.matching.engine import ENGINE_VERSION, ReconciliationEngine
from concordance.matching.scoring_config import ScoringConfig
from concordance.matching.strategies import build_strategy

log = get_logger("jobs.replay")

#: Confidence is a float that travelled through Postgres and back, so exact
#: equality would report drift on the last bit. A decision that changed is the
#: finding; a confidence that moved by less than this is arithmetic.
CONFIDENCE_TOLERANCE = 1e-9


class SnapshotDriftError(RuntimeError):
    """The data no longer matches the snapshot the run was decided against."""


@dataclass
class Drift:
    """One record the replay decided differently."""

    record_id: str
    field: str
    before: Any
    after: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "field": self.field,
            "before": self.before,
            "after": self.after,
        }


@dataclass
class ReplayReport:
    run_id: uuid.UUID
    engine_version_then: str
    engine_version_now: str
    scoring_config: str
    strategy: str
    compared: int = 0
    identical: int = 0
    drifted: int = 0
    missing: int = 0
    unexpected: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    seconds: float = 0.0
    hashes_match: bool = True
    drifts: list[Drift] = field(default_factory=list)

    @property
    def decision_identical(self) -> bool:
        return self.drifted == 0 and self.missing == 0 and self.unexpected == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "decision_identical": self.decision_identical,
            "engine_version_then": self.engine_version_then,
            "engine_version_now": self.engine_version_now,
            "scoring_config": self.scoring_config,
            "strategy": self.strategy,
            "compared": self.compared,
            "identical": self.identical,
            "drifted": self.drifted,
            "missing": self.missing,
            "unexpected": self.unexpected,
            "llm_calls": self.llm_calls,
            "cache_hits": self.cache_hits,
            "hashes_match": self.hashes_match,
            "seconds": round(self.seconds, 2),
            "drifts": [d.as_dict() for d in self.drifts[:50]],
        }

    def lines(self, sample: int = 10) -> list[str]:
        verdict = "IDENTICAL" if self.decision_identical else "DRIFTED"
        out = [
            f"replay {self.run_id}  {verdict}",
            f"  strategy       {self.strategy}",
            f"  config         {self.scoring_config}",
            f"  engine         {self.engine_version_then} -> {self.engine_version_now}",
            f"  compared       {self.compared}  identical {self.identical}  drifted {self.drifted}",
            f"  missing        {self.missing}   unexpected {self.unexpected}",
            f"  llm calls      {self.llm_calls}  (cache hits {self.cache_hits})",
            f"  snapshot       {'unchanged' if self.hashes_match else 'CHANGED'}",
            f"  seconds        {self.seconds:.2f}",
        ]
        for drift in self.drifts[:sample]:
            out.append(
                f"    {drift.record_id}  {drift.field}: {drift.before!r} -> {drift.after!r}"
            )
        return out


def replay(
    session: Session,
    settings: Settings,
    run_id: uuid.UUID,
    *,
    force: bool = False,
    show_progress: bool = False,
) -> ReplayReport:
    """Re-execute `run_id` from its recorded provenance and compare decisions."""
    from concordance.eval.pairs import stream_from_postgres

    started = time.perf_counter()
    run = session.get(ReconciliationRun, run_id)
    if run is None:
        raise LookupError(f"no run {run_id}")

    config_row = _config_of(session, run)
    scoring = ScoringConfig.from_dict(config_row.params)
    request = RunRequest.from_payload({**dict(run.request or {}), "offline_llm": True})
    request.strategy = run.strategy
    request.show_progress = show_progress

    adjudicator = build_adjudicator_for(session, settings, request)
    strategy = build_strategy(request.strategy, scoring.engine(), adjudicator)
    engine = ReconciliationEngine(strategy=strategy)

    stream = stream_from_postgres(
        session,
        max_candidates=request.max_candidates or settings.MAX_CANDIDATES_PER_RECORD,
        limit=request.limit,
        chunk_size=request.chunk_size,
        with_truth=False,
        show_progress=show_progress,
    )

    provider_hash = stream.store.snapshot_hash()
    sanction_hash = stream.store.sanction_snapshot_hash()
    hashes_match = (
        provider_hash == run.provider_snapshot_hash
        and sanction_hash == run.sanction_snapshot_hash
    )
    if not hashes_match and not force:
        raise SnapshotDriftError(
            f"run {run_id} was decided against provider snapshot "
            f"{run.provider_snapshot_hash} / sanction snapshot {run.sanction_snapshot_hash}; "
            f"the database now holds {provider_hash} / {sanction_hash}. "
            "Replay would not be comparing like with like - pass force to see it anyway."
        )

    report = ReplayReport(
        run_id=run_id,
        engine_version_then=run.engine_version or "",
        engine_version_now=engine.engine_version,
        scoring_config=config_row.version,
        strategy=request.strategy,
        hashes_match=hashes_match,
    )
    if run.engine_version and run.engine_version != ENGINE_VERSION:
        log.warning(
            "replay.engine_version_changed",
            run_id=str(run_id),
            then=run.engine_version,
            now=ENGINE_VERSION,
        )

    original = _original_decisions(session, run_id)
    seen: set[str] = set()

    for batch in stream.chunks():
        work = [(w.record_id, w.normalized, w.candidates) for w in batch]
        for outcome in engine.decide_many(work):
            seen.add(outcome.record_id)
            before = original.get(outcome.record_id)
            if before is None:
                # The original run never decided this record - a limit that
                # changed, or a record loaded since. Not a drift; its own count.
                report.unexpected += 1
                continue
            if outcome.result is None:
                report.drifts.append(Drift(outcome.record_id, "error", before[0], outcome.error))
                report.drifted += 1
                report.compared += 1
                continue
            report.compared += 1
            drifts = _compare(outcome.record_id, before, outcome.result)
            if drifts:
                report.drifts.extend(drifts)
                report.drifted += 1
            else:
                report.identical += 1

    report.missing = len(set(original) - seen)
    stats = strategy.stats()
    report.llm_calls = int(stats.get("adjudicated") or 0)
    router = stats.get("router") or {}
    report.cache_hits = int(router.get("cache_hits") or 0)
    report.seconds = time.perf_counter() - started
    log.info("replay.done", **report.as_dict())
    return report


def _config_of(session: Session, run: ReconciliationRun) -> ScoringConfigRow:
    """The exact weights the run decided with - never the latest ones."""
    if run.scoring_config_id is None:
        raise LookupError(f"run {run.id} records no scoring config; it cannot be replayed")
    config = session.get(ScoringConfigRow, run.scoring_config_id)
    if config is None:
        raise LookupError(f"scoring config {run.scoring_config_id} is gone; cannot replay")
    return config


def _original_decisions(
    session: Session, run_id: uuid.UUID
) -> dict[str, tuple[str, str | None, float | None]]:
    """What the run decided, keyed by the business record id."""
    rows = session.execute(
        select(
            SanctionRecordRow.record_id,
            MatchResult.decision,
            MatchResult.chosen_provider_id,
            MatchResult.calibrated_confidence,
        )
        .join(MatchResult, MatchResult.sanction_record_id == SanctionRecordRow.id)
        .where(MatchResult.run_id == run_id)
    )
    return {
        record_id: (decision, provider_id, confidence)
        for record_id, decision, provider_id, confidence in rows
    }


def _compare(
    record_id: str, before: tuple[str, str | None, float | None], after: Any
) -> list[Drift]:
    decision, provider_id, confidence = before
    drifts: list[Drift] = []
    if decision != str(after.decision):
        drifts.append(Drift(record_id, "decision", decision, str(after.decision)))
    if provider_id != after.chosen_provider_id:
        drifts.append(Drift(record_id, "chosen_provider_id", provider_id, after.chosen_provider_id))
    if confidence is not None and abs(confidence - after.confidence) > CONFIDENCE_TOLERANCE:
        drifts.append(Drift(record_id, "confidence", confidence, after.confidence))
    return drifts


__all__ = ["CONFIDENCE_TOLERANCE", "Drift", "ReplayReport", "SnapshotDriftError", "replay"]
