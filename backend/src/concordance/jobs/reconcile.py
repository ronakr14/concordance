"""A reconciliation run: the engine, Postgres on both sides, and provenance.

This is the half of the pipeline that knows about storage, which is why it is
here rather than in `matching/`. It reads providers and sanction records from
Postgres, blocks them with the SQL generator, hands each chunk to
`ReconciliationEngine`, and writes the decisions back.

Three things it does that a straight loop would not:

**It pins the run to its inputs.** Engine version, scoring config, prompt
version and both snapshot hashes go onto the run row before any record is
scored. That set is what `replay` checks, and a replay that cannot prove the
inputs are unchanged refuses rather than producing a number nobody can trust.

**It supersedes rather than overwrites (Q5).** A re-run writes new rows and
points the previous ones at them. Nothing is deleted, `superseded_by IS NULL`
still means "current", and the decision an active case was opened on remains
exactly as it was.

**It never touches an active case.** A re-run that disagrees with a case a
human opened raises a flag on that case and stops there. Closing it
automatically would let a config change silently retract a human decision; the
flag puts the disagreement in front of the human instead.

Results are persisted per chunk rather than at the end, so a run that dies at
record four thousand leaves four thousand decisions and a `FAILED` run row that
says why - not an empty table.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from concordance.config import Settings
from concordance.db.enums import CaseStatus, RunStatus
from concordance.db.enums import Route as DbRoute
from concordance.db.models import Case, LlmCall, MatchCandidate, MatchResult, ReconciliationRun
from concordance.db.models import SanctionRecord as SanctionRecordRow
from concordance.db.models import ScoringConfig as ScoringConfigRow
from concordance.db.repositories.audit import AuditRepository
from concordance.db.repositories.cases import CaseRepository
from concordance.db.repositories.matches import MatchRepository
from concordance.logging_setup import get_logger
from concordance.matching.engine import ReconciliationEngine
from concordance.matching.scorer import Route
from concordance.matching.scoring_config import ScoringConfig
from concordance.matching.strategies import StrategyName, build_strategy

log = get_logger("jobs.reconcile")

#: The engine has a fourth route that is a reason rather than a decision path,
#: and the column CHECK constraint says so. The real route survives in
#: `explanation.route`.
_ROUTE_TO_DB = {
    Route.DETERMINISTIC: DbRoute.DETERMINISTIC,
    Route.PROBABILISTIC: DbRoute.PROBABILISTIC,
    Route.LLM: DbRoute.LLM,
    Route.NO_CANDIDATES: DbRoute.PROBABILISTIC,
}


@dataclass
class RunRequest:
    """What a run does. Nothing here is about how the run is driven."""

    strategy: str = str(StrategyName.PROBABILISTIC)
    limit: int | None = None
    config_version: str | None = None
    chunk_size: int = 500
    max_candidates: int | None = None
    file_id: uuid.UUID | None = None
    triggered_by: uuid.UUID | None = None
    #: Replay mode: the adjudicator may answer only from the cache.
    offline_llm: bool = False
    show_progress: bool = True

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RunRequest:
        """A job payload, which is how the worker receives one."""
        return cls(
            strategy=str(payload.get("strategy") or StrategyName.PROBABILISTIC),
            limit=payload.get("limit"),
            config_version=payload.get("config_version"),
            chunk_size=int(payload.get("chunk_size") or 500),
            max_candidates=payload.get("max_candidates"),
            file_id=_as_uuid(payload.get("file_id")),
            triggered_by=_as_uuid(payload.get("triggered_by")),
            offline_llm=bool(payload.get("offline_llm", False)),
            show_progress=bool(payload.get("show_progress", False)),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "limit": self.limit,
            "config_version": self.config_version,
            "chunk_size": self.chunk_size,
            "max_candidates": self.max_candidates,
            "file_id": str(self.file_id) if self.file_id else None,
            "triggered_by": str(self.triggered_by) if self.triggered_by else None,
            "offline_llm": self.offline_llm,
        }


@dataclass
class RunReport:
    """A finished run, in the shape worth printing."""

    run_id: uuid.UUID
    status: str
    counters: dict[str, Any]
    engine_version: str
    scoring_config_version: str
    prompt_version: str
    provider_snapshot_hash: str
    sanction_snapshot_hash: str
    seconds: float = 0.0
    superseded: int = 0
    conflicts: int = 0
    skipped_unknown: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "status": self.status,
            "engine_version": self.engine_version,
            "scoring_config": self.scoring_config_version,
            "prompt_version": self.prompt_version,
            "provider_snapshot_hash": self.provider_snapshot_hash,
            "sanction_snapshot_hash": self.sanction_snapshot_hash,
            "seconds": round(self.seconds, 2),
            "superseded": self.superseded,
            "conflicts": self.conflicts,
            "skipped_unknown": self.skipped_unknown,
            "errors": self.errors[:20],
            **self.counters,
        }


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# --------------------------------------------------------------------------
# the scoring configuration
# --------------------------------------------------------------------------


def ensure_scoring_config(
    session: Session, settings: Settings, version: str | None = None
) -> ScoringConfigRow:
    """The fitted config this run scores with, as a row.

    A config that exists only as a file on the machine that fitted it cannot be
    the provenance of anything: the run row has to point at something a second
    machine can read. So a file-backed config is imported on first use, and the
    row is immutable from then on - refitting means a new version, never an edit.
    """
    repo = MatchRepository(session)
    if version:
        row = repo.get_config(version)
        if row is None:
            raise LookupError(f"no scoring config {version!r} in the database")
        return row

    row = repo.latest_config()
    if row is not None:
        return row

    path = _latest_config_file(settings)
    if path is None:
        raise LookupError(
            "no scoring config in the database and none on disk - fit one first"
        )
    loaded = ScoringConfig.read(path)
    return import_scoring_config(session, loaded, notes=f"imported from {path.name}")


def import_scoring_config(
    session: Session, config: ScoringConfig, *, notes: str | None = None
) -> ScoringConfigRow:
    """Write a fitted config to `scoring_configs`, or return the row already there."""
    existing = MatchRepository(session).get_config(config.config_id)
    if existing is not None:
        return existing

    thresholds = config.bundles[next(iter(config.bundles))].thresholds.as_dict()
    row = ScoringConfigRow(
        version=config.config_id,
        params=config.as_dict(),
        t_auto_accept=float(thresholds["t_auto_accept"]),
        t_auto_reject=float(thresholds["t_auto_reject"]),
        calibrator={str(k): b.calibrator.as_dict() for k, b in config.bundles.items()},
        fitted_from=config.fitted_from,
        notes=notes,
    )
    session.add(row)
    # Committed here, not left pending for the run's transaction. The snapshot
    # hashes are read next, and a reconnect during those rolls the session back
    # - which used to take this insert with it and leave the run row pointing
    # at a scoring config that no longer existed. Registering a config is
    # idempotent by version, so committing it early costs nothing.
    session.commit()
    log.info("reconcile.config.imported", version=row.version, id=str(row.id))
    return row


def _latest_config_file(settings: Settings) -> Path | None:
    configs = sorted((settings.DATA_DIR / "configs").glob("config_*.json"))
    return configs[-1] if configs else None


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def reconcile(session: Session, settings: Settings, request: RunRequest) -> RunReport:
    """Score every sanction record against the provider master, and persist it."""
    from concordance.eval.pairs import stream_from_postgres

    started = time.perf_counter()
    config_row = ensure_scoring_config(session, settings, request.config_version)
    scoring = ScoringConfig.from_dict(config_row.params)
    adjudicator = build_adjudicator_for(session, settings, request)
    strategy = build_strategy(request.strategy, scoring.engine(), adjudicator)
    engine = ReconciliationEngine(strategy=strategy)

    stream = stream_from_postgres(
        session,
        max_candidates=request.max_candidates or settings.MAX_CANDIDATES_PER_RECORD,
        limit=request.limit,
        chunk_size=request.chunk_size,
        with_truth=False,
        show_progress=request.show_progress,
    )
    prompt_version = str(getattr(adjudicator, "prompt_version", "none"))

    run = ReconciliationRun(
        triggered_by=request.triggered_by,
        file_id=request.file_id,
        status=str(RunStatus.RUNNING),
        started_at=datetime.now(UTC),
        engine_version=engine.engine_version,
        scoring_config_id=config_row.id,
        prompt_version=prompt_version,
        strategy=request.strategy,
        request=request.as_payload(),
        provider_snapshot_hash=stream.store.snapshot_hash(),
        sanction_snapshot_hash=stream.store.sanction_snapshot_hash(),
    )
    session.add(run)
    session.commit()
    log.info(
        "reconcile.start",
        run_id=str(run.id),
        records=len(stream),
        strategy=request.strategy,
        config=config_row.version,
        engine_version=engine.engine_version,
    )

    writer = _ResultWriter(session, run.id)
    report = RunReport(
        run_id=run.id,
        status=str(RunStatus.RUNNING),
        counters={},
        engine_version=engine.engine_version,
        scoring_config_version=config_row.version,
        prompt_version=prompt_version,
        provider_snapshot_hash=run.provider_snapshot_hash or "",
        sanction_snapshot_hash=run.sanction_snapshot_hash or "",
    )

    try:
        for batch in stream.chunks():
            work = [(w.record_id, w.normalized, w.candidates) for w in batch]
            for outcome in engine.decide_many(work):
                if outcome.failed or outcome.result is None:
                    report.errors.append(f"{outcome.record_id}: {outcome.error}")
                    log.warning(
                        "reconcile.record.failed",
                        run_id=str(run.id),
                        record_id=outcome.record_id,
                        error=outcome.error,
                    )
                    continue
                writer.write(outcome.result, adjudicator)
            writer.flush_chunk()
            _progress(run, engine)
            # Committing per chunk is what makes the progress counts on the run
            # row readable while the run is still going, and what leaves the
            # first four thousand decisions behind when a run dies at the
            # four-thousand-and-first.
            session.commit()
    except Exception as exc:
        # The failing statement may have poisoned the transaction, so the
        # status is written on a clean one. A run that ends without saying it
        # failed is worse than the failure.
        session.rollback()
        failed = session.get(ReconciliationRun, run.id)
        if failed is not None:
            failed.status = str(RunStatus.FAILED)
            failed.finished_at = datetime.now(UTC)
            failed.error = f"{type(exc).__name__}: {exc}"
            _finalize(failed, engine)
            session.commit()
        log.error("reconcile.failed", run_id=str(run.id), error=f"{type(exc).__name__}: {exc}")
        raise

    engine.counters.absorb_adjudicator(strategy.stats())
    run.status = str(RunStatus.COMPLETED)
    run.finished_at = datetime.now(UTC)
    _finalize(run, engine)
    session.commit()

    report.status = run.status
    report.counters = engine.counters.as_dict()
    report.seconds = time.perf_counter() - started
    report.superseded = writer.superseded
    report.conflicts = writer.conflicts
    report.skipped_unknown = writer.skipped_unknown
    log.info("reconcile.done", **report.as_dict())
    return report


def build_adjudicator_for(session: Session, settings: Settings, request: RunRequest) -> Any:
    """The adjudicator for this run, cached against `llm_calls`.

    Grey-band records reach it only under `probabilistic_llm`; every other
    strategy gets `None` and `build_strategy` ignores it, so a deterministic run
    never opens an HTTP client.
    """
    if StrategyName(request.strategy) is not StrategyName.PROBABILISTIC_LLM:
        return None
    from concordance.llm.ai_matcher import build_adjudicator
    from concordance.llm.router import LLMRouter
    from concordance.store.postgres_cache import PostgresCache

    router = LLMRouter.from_settings(
        settings, cache=PostgresCache(session), offline=request.offline_llm
    )
    return build_adjudicator(settings, router=router)


def _progress(run: ReconciliationRun, engine: ReconciliationEngine) -> None:
    """Counts onto the run row as the run goes, so a watcher sees movement."""
    counters = engine.counters
    run.records_total = counters.records_total
    run.matched_count = counters.matched
    run.ambiguous_count = counters.ambiguous
    run.no_match_count = counters.no_match


def _finalize(run: ReconciliationRun, engine: ReconciliationEngine) -> None:
    _progress(run, engine)
    run.llm_calls = engine.counters.llm_calls
    run.llm_tokens = engine.counters.llm_tokens
    run.llm_cost_usd = engine.counters.llm_cost_usd


class _ResultWriter:
    """Writes results and candidates, supersedes the previous ones, flags conflicts.

    The record-id lookup, the current-result lookup and the active cases are
    each loaded once for the whole run. Per record they would be three more
    round trips on top of the blocking queries, which at five thousand records
    is the difference between a run that finishes over lunch and one that does
    not.
    """

    def __init__(self, session: Session, run_id: uuid.UUID) -> None:
        self.session = session
        self.run_id = run_id
        self.matches = MatchRepository(session)
        self.cases = CaseRepository(session)
        self.audit = AuditRepository(session)
        self.superseded = 0
        self.conflicts = 0
        self.skipped_unknown = 0
        self._ids: dict[str, uuid.UUID] = {
            record_id: row_id
            for row_id, record_id in session.execute(
                select(SanctionRecordRow.id, SanctionRecordRow.record_id)
            )
        }
        self._current: dict[uuid.UUID, uuid.UUID] = {
            sanction_id: result_id
            for result_id, sanction_id in session.execute(
                select(MatchResult.id, MatchResult.sanction_record_id).where(
                    MatchResult.superseded_by.is_(None)
                )
            )
        }
        # A list per record, not one case: the partial unique index allows one
        # active case per provider *and* record, so a record matched to two
        # providers can have two live cases, and a re-run that disagrees
        # disagrees with both.
        self._active_cases: dict[uuid.UUID, list[Case]] = {}
        for case in session.scalars(select(Case).where(Case.status == CaseStatus.ACTIVE)):
            self._active_cases.setdefault(case.sanction_record_id, []).append(case)
        self._llm_calls: dict[str, uuid.UUID] = {}
        # Candidates are held back until their results have been inserted.
        # SQLAlchemy orders a flush from ORM relationships, and these tables are
        # joined by a plain foreign-key column rather than a `relationship()`,
        # so nothing tells the unit of work that a candidate depends on its
        # result - and the first chunk went in child-first, which the database
        # refused. The same shape of bug bit the Stage 5 loader.
        self._pending_candidates: list[MatchCandidate] = []
        # Supersede and conflict both point *at* a result that is still pending,
        # and an UPDATE runs immediately while an ORM insert waits for the
        # flush. Deferring them to `flush_chunk` is what keeps those foreign
        # keys pointing at rows that exist.
        self._pending_links: list[Callable[[], None]] = []

    def write(self, result: Any, adjudicator: Any) -> MatchResult | None:
        sanction_id = self._ids.get(result.record_id)
        if sanction_id is None:
            # The dataset held a record the database does not. Skipping it is
            # right; silence is not, so the count is reported on the run.
            self.skipped_unknown += 1
            log.warning("reconcile.record.unknown", record_id=result.record_id)
            return None

        row = MatchResult(
            # The key is generated here rather than by a flush: a round trip
            # per record is four thousand round trips per run, which over a
            # network link costs more than all the scoring put together.
            id=uuid.uuid4(),
            run_id=self.run_id,
            sanction_record_id=sanction_id,
            decision=str(result.decision),
            chosen_provider_id=result.chosen_provider_id,
            posterior=result.top.posterior if result.top else None,
            calibrated_confidence=result.confidence,
            raw_match_weight=result.match_weight,
            route=str(_ROUTE_TO_DB[result.route]),
            llm_call_id=self._llm_call_id(result, adjudicator),
            explanation=self._explanation(result),
        )
        self.session.add(row)

        for candidate in result.candidates:
            self._pending_candidates.append(
                MatchCandidate(
                    match_result_id=row.id,
                    provider_id=candidate.provider_id,
                    rank=candidate.rank,
                    field_levels=dict(candidate.levels),
                    field_weights={
                        str(f["field"]): float(f["weight"]) for f in candidate.field_weights
                    },
                    match_weight=candidate.match_weight,
                    posterior=candidate.posterior,
                    blocking_keys=list(candidate.blocking_keys),
                )
            )

        self._flag_conflict(sanction_id, row)
        self._supersede(sanction_id, row)
        self._current[sanction_id] = row.id
        return row

    def flush_chunk(self) -> None:
        """Insert this chunk's results, then everything that references them."""
        self.session.flush()
        if self._pending_candidates:
            self.session.add_all(self._pending_candidates)
            self._pending_candidates.clear()
        for link in self._pending_links:
            link()
        self._pending_links.clear()
        self.session.flush()

    def _supersede(self, sanction_id: uuid.UUID, row: MatchResult) -> None:
        previous = self._current.get(sanction_id)
        if previous is None or previous == row.id:
            return
        self._pending_links.append(partial(self.matches.supersede, previous, row.id))
        self.superseded += 1

    def _flag_conflict(self, sanction_id: uuid.UUID, row: MatchResult) -> None:
        """A re-run that disagrees with an open case raises a flag, nothing more (Q5)."""
        cases = [c for c in self._active_cases.get(sanction_id, ()) if not c.conflict_flag]
        if not cases:
            return
        previous_id = self._current.get(sanction_id)
        if previous_id is None or previous_id == row.id:
            return
        previous = self.matches.get_result(previous_id)
        if previous is None:
            return
        if (
            previous.decision == row.decision
            and previous.chosen_provider_id == row.chosen_provider_id
        ):
            return

        before = {
            "decision": previous.decision,
            "chosen_provider_id": previous.chosen_provider_id,
            "match_result_id": str(previous.id),
        }
        after = {
            "decision": row.decision,
            "chosen_provider_id": row.chosen_provider_id,
            "match_result_id": str(row.id),
            "run_id": str(self.run_id),
        }
        for case in cases:
            case.conflict_flag = True  # so a second record in the same chunk sees it
            self.conflicts += 1
            self._pending_links.append(partial(self._write_conflict, case.id, row.id, before, after))

    def _write_conflict(
        self,
        case_id: uuid.UUID,
        result_id: uuid.UUID,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        self.cases.flag_conflict(case_id, result_id)
        self.audit.record(
            action="case.conflict_flagged",
            entity_type="case",
            entity_id=str(case_id),
            actor_role="system",
            before=before,
            after=after,
        )
        log.info("reconcile.case.conflict", case_id=str(case_id), run_id=str(self.run_id))

    def _llm_call_id(self, result: Any, adjudicator: Any) -> uuid.UUID | None:
        """Link the result to the adjudication row, when an adjudicator answered.

        `last_call_key` is the cache key of the call that decided this record,
        and the cache key is `llm_calls.cache_key`, so the join is exact rather
        than "the most recent call, probably".
        """
        if result.route is not Route.LLM or adjudicator is None:
            return None
        key = getattr(adjudicator, "last_call_key", None)
        if not key:
            return None
        if key not in self._llm_calls:
            found = self.session.scalar(select(LlmCall.id).where(LlmCall.cache_key == key))
            if found is None:
                return None
            self._llm_calls[key] = found
        return self._llm_calls[key]

    @staticmethod
    def _explanation(result: Any) -> dict[str, Any]:
        return {
            "route": str(result.route),
            "reason": str(result.reason),
            "model": str(result.kind),
            "margin": result.margin,
            "notes": list(result.notes),
            "candidates": [c.as_dict() for c in result.candidates],
        }


__all__ = [
    "RunReport",
    "RunRequest",
    "build_adjudicator_for",
    "ensure_scoring_config",
    "import_scoring_config",
    "reconcile",
]
