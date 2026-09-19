"""Requesting, running and reading Lab experiments.

Two kinds, one table (`lab_sweeps`), and the same shape as a reconciliation
run: the row exists in `QUEUED` before any work starts, a job names it, and the
worker adopts the row rather than creating its own - so the id a page polls is
the id that ends up holding the results.

- **sweep** - every corruption level by every non-LLM strategy, with the fit's
  before/after calibration. Minutes of CPU, no network.
- **llm** - the routed-versus-everything sample (`eval.llm_experiment`) at a
  few levels of a finished sweep. Few CPU seconds, many rate-limited model
  calls, so it is a separate experiment rather than a phase of the sweep: a
  sweep should not take an hour because a free tier is busy, and an LLM run
  that stops half-way is resumed from the response cache for free.

**One live experiment at a time**, across both kinds. A sweep holds four
50,000-provider indexes in memory and an LLM run holds a provider's rate limit;
running two of either at once only makes both slower.

**A failure is recorded on the row before it is raised**, in its own commit,
because the worker rolls the handler's transaction back and a row left in
`RUNNING` would read as a sweep that is still going.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from concordance.audit import service as audit
from concordance.audit.service import Actor
from concordance.config import Settings
from concordance.db.enums import JobStatus, LabKind, RunStatus
from concordance.db.models import EvalRun, Job, LabSweep
from concordance.db.repositories.jobs import JobRepository
from concordance.errors import ConflictError, InvalidError, NotFoundError
from concordance.logging_setup import get_logger
from concordance.matching.strategies import StrategyName

log = get_logger("lab.service")

LIVE = (str(RunStatus.QUEUED), str(RunStatus.RUNNING))
SWEEP_STRATEGIES: tuple[str, ...] = (
    str(StrategyName.DETERMINISTIC),
    str(StrategyName.FUZZY),
    str(StrategyName.PROBABILISTIC),
)
DEFAULT_LEVELS: tuple[float, ...] = tuple(round(i / 10, 1) for i in range(10))
DEFAULT_LLM_LEVELS: tuple[float, ...] = (0.3, 0.5, 0.7)
DEFAULT_LLM_SAMPLE = 100
#: A jobless experiment silent for this long is treated as dead. See `effective_status`.
STALE_AFTER = timedelta(minutes=30)


# --------------------------------------------------------------------------
# requesting
# --------------------------------------------------------------------------


def _live(session: Session) -> LabSweep | None:
    """The experiment that is genuinely queued or running, if any.

    Read through `effective_status`, so a row whose process died cannot block
    every later experiment by still saying `RUNNING`.
    """
    rows = session.scalars(
        select(LabSweep).where(LabSweep.status.in_(LIVE)).order_by(LabSweep.created_at.desc())
    )
    return next((row for row in rows if effective_status(session, row)[0] in LIVE), None)


def _refuse_if_live(session: Session) -> None:
    live = _live(session)
    if live is not None:
        raise ConflictError(
            f"a Lab {live.kind} is already {live.status.lower()}",
            details={"lab_id": str(live.id), "kind": live.kind, "status": live.status},
        )


def _levels(values: list[float] | tuple[float, ...] | None, default: tuple[float, ...]) -> list[float]:
    chosen = sorted({round(float(v), 1) for v in (values or default)})
    bad = [v for v in chosen if not 0.0 <= v <= 0.9]
    if bad:
        raise InvalidError(
            "corruption levels must lie between 0.0 and 0.9",
            details={"levels": f"out of range: {bad}"},
        )
    return chosen


def request_sweep(
    session: Session,
    settings: Settings,
    actor: Actor,
    *,
    levels: list[float] | None = None,
    providers: int | None = None,
    sanctions: int | None = None,
    seed: int | None = None,
    enqueue: bool = True,
) -> LabSweep:
    """Queue a sweep and return its row at once. The caller commits.

    `enqueue=False` writes the row without a job, for a caller that runs it
    in-process (the CLI) and must not race a worker for it.
    """
    _refuse_if_live(session)
    params: dict[str, Any] = {
        "levels": _levels(levels, DEFAULT_LEVELS),
        "strategies": list(SWEEP_STRATEGIES),
        "providers": int(providers or 50_000),
        "sanctions": int(sanctions or 5_000),
        "seed": int(seed or settings.RANDOM_SEED),
        "max_candidates": settings.MAX_CANDIDATES_PER_RECORD,
        "target_precision": settings.TARGET_PRECISION,
        "workers": settings.LAB_SWEEP_WORKERS,
        "root": str(settings.DATA_DIR / "sweep"),
    }
    return _queue(session, actor, LabKind.SWEEP, params, parent_id=None, enqueue=enqueue)


def request_llm(
    session: Session,
    settings: Settings,
    actor: Actor,
    *,
    sweep_id: uuid.UUID | None = None,
    levels: list[float] | None = None,
    sample: int | None = None,
    enqueue: bool = True,
) -> LabSweep:
    """Queue an LLM experiment on a finished sweep's datasets. The caller commits."""
    if not settings.LLM_ENABLED:
        raise ConflictError(
            "the LLM is disabled (LLM_ENABLED=false), so there is nothing to measure",
            code="llm_disabled",
        )
    _refuse_if_live(session)
    parent = (
        session.get(LabSweep, sweep_id)
        if sweep_id is not None
        else latest(session, LabKind.SWEEP, completed=True)
    )
    if parent is None or parent.kind != LabKind.SWEEP:
        raise NotFoundError("no finished sweep to extend; run a sweep first")
    if parent.status != RunStatus.COMPLETED:
        raise ConflictError(
            f"sweep {parent.id} is {parent.status.lower()}, not completed",
            details={"sweep_id": str(parent.id)},
        )
    chosen = _levels(levels, DEFAULT_LLM_LEVELS)
    missing = [v for v in chosen if v not in parent.params.get("levels", [])]
    if missing:
        raise InvalidError(
            "the sweep did not measure every requested level",
            details={"levels": f"not in the sweep: {missing}"},
        )
    size = int(sample or DEFAULT_LLM_SAMPLE)
    if not 10 <= size <= 1_000:
        raise InvalidError(
            "sample must be between 10 and 1000 records per stratum",
            details={"sample": str(size)},
        )
    params = {
        "levels": chosen,
        "sample_per_stratum": size,
        "price_model": settings.LAB_PRICE_MODEL,
        "top_k": settings.LLM_TOP_K,
        "chain": settings.LLM_PROVIDER_CHAIN,
    }
    return _queue(session, actor, LabKind.LLM, params, parent_id=parent.id, enqueue=enqueue)


def _queue(
    session: Session,
    actor: Actor,
    kind: LabKind,
    params: dict[str, Any],
    parent_id: uuid.UUID | None,
    enqueue: bool = True,
) -> LabSweep:
    row = LabSweep(
        kind=str(kind),
        parent_id=parent_id,
        status=str(RunStatus.QUEUED),
        requested_by=actor.user_id,
        params=params,
        progress={},
        summary={},
    )
    session.add(row)
    session.flush()
    if enqueue:
        # One attempt: a half-written experiment is recorded as failed and rerun
        # by asking again, never retried silently under the same id.
        job = JobRepository(session).enqueue(
            f"lab_{kind}", {"lab_id": str(row.id)}, max_attempts=1
        )
        session.flush()
        row.job_id = job.id
    audit.record(
        session,
        actor,
        f"lab.{kind}_requested",
        entity_type="lab_sweep",
        entity_id=row.id,
        after={"kind": str(kind), "params": params, "parent_id": str(parent_id) if parent_id else None},
    )
    return row


# --------------------------------------------------------------------------
# running (the worker calls these)
# --------------------------------------------------------------------------


def _adopt(session: Session, lab_id: uuid.UUID, kind: LabKind) -> LabSweep:
    row = session.get(LabSweep, lab_id)
    if row is None or row.kind != kind:
        raise NotFoundError(f"no Lab {kind} {lab_id}")
    row.status = str(RunStatus.RUNNING)
    row.started_at = datetime.now(UTC)
    row.error = None
    session.commit()
    return row


def _progress(session: Session, row: LabSweep, progress: dict[str, Any]) -> None:
    """Best effort. A dropped connection must not fail minutes of measurement."""
    row.progress = progress
    try:
        session.commit()
    except OperationalError as exc:
        session.rollback()
        log.warning("lab.progress_not_saved", lab_id=str(row.id), error=str(exc).splitlines()[0])


def _fail(session: Session, lab_id: uuid.UUID, exc: BaseException) -> None:
    session.rollback()
    row = session.get(LabSweep, lab_id)
    if row is None:
        return
    row.status = str(RunStatus.FAILED)
    row.error = f"{type(exc).__name__}: {exc}"[:2000]
    row.finished_at = datetime.now(UTC)
    session.commit()


def run_sweep(
    session: Session,
    settings: Settings,  # noqa: ARG001 - the handler signature; params are on the row
    lab_id: uuid.UUID,
) -> dict[str, Any]:
    from concordance.eval.sweep import sweep

    row = _adopt(session, lab_id, LabKind.SWEEP)
    params = dict(row.params)
    try:
        _progress(session, row, {"done": 0, "total": len(params["levels"])})

        def on_level(_corruption: float, done: int, total: int) -> None:
            _progress(session, row, {"done": done, "total": total})

        result = sweep(
            root=Path(params["root"]),
            seed=int(params["seed"]),
            levels=tuple(params["levels"]),
            strategies=tuple(StrategyName(s) for s in params["strategies"]),
            providers=int(params["providers"]),
            sanctions=int(params["sanctions"]),
            max_candidates=int(params["max_candidates"]),
            target_precision=float(params["target_precision"]),
            workers=int(params["workers"]),
            on_level=on_level,
        )
        for cell in result.cells:
            session.add(_eval_row(cell, sweep_id=row.id))
        row.status = str(RunStatus.COMPLETED if result.cells else RunStatus.FAILED)
        row.finished_at = datetime.now(UTC)
        row.summary = {
            "seconds": round(result.seconds, 1),
            "cells": len(result.cells),
            "errors": result.errors,
        }
        if not result.cells:
            row.error = "every level failed; see summary.errors"
        session.commit()
    except Exception as exc:
        _fail(session, lab_id, exc)
        raise
    return {"lab_id": str(lab_id), "cells": len(result.cells), "errors": len(result.errors)}


def run_llm(session: Session, settings: Settings, lab_id: uuid.UUID) -> dict[str, Any]:
    from concordance.eval.fitting import fit_config
    from concordance.eval.llm_experiment import MeteredRouter, TokenMeter, run_llm_experiment
    from concordance.eval.pairs import prepare
    from concordance.eval.sweep import _dataset_dir, _reusable
    from concordance.llm.ai_matcher import LlmAdjudicator
    from concordance.llm.cache import FileCache
    from concordance.llm.pricing import PriceTable
    from concordance.llm.router import LLMRouter
    from concordance.synth.pipeline import seed_dataset

    row = _adopt(session, lab_id, LabKind.LLM)
    params = dict(row.params)
    parent = session.get(LabSweep, row.parent_id) if row.parent_id else None
    started = time.perf_counter()
    try:
        if parent is None:
            raise NotFoundError("the sweep this experiment extends no longer exists")
        base = dict(parent.params)
        seed = int(base["seed"])
        price_model = str(params["price_model"])
        price = PriceTable.load(settings.LLM_PRICE_TABLE).get(price_model)
        router = LLMRouter.from_settings(settings, cache=FileCache(settings.llm_cache_dir))
        if not router.available:
            raise ConflictError(f"no usable LLM provider in chain {router.chain_names()}")

        levels = list(params["levels"])
        sample = int(params["sample_per_stratum"])
        written = 0
        try:
            for position, level in enumerate(levels):
                dataset = _dataset_dir(Path(base["root"]), level)
                if not _reusable(dataset, seed, level, int(base["providers"]), int(base["sanctions"])):
                    seed_dataset(
                        providers=int(base["providers"]),
                        sanctions=int(base["sanctions"]),
                        corruption=level,
                        seed=seed,
                        out_dir=dataset,
                        write_excel=False,
                    )
                prepared = prepare(
                    dataset, max_candidates=int(base["max_candidates"]), show_progress=False
                )
                fit = fit_config(
                    prepared, seed=seed, target_precision=float(base["target_precision"])
                )
                meter = TokenMeter()
                adjudicator = LlmAdjudicator(
                    router=MeteredRouter(router, meter),  # type: ignore[arg-type]
                    top_k=int(params["top_k"]),
                )

                def on_progress(done: int, total: int, position: int = position, level: float = level) -> None:
                    # A commit per call would be one round trip to a hosted
                    # database per model call; every fifth is plenty to watch.
                    if done % 5 == 0 or done == total:
                        _progress(
                            session,
                            row,
                            {
                                "level": level,
                                "levels_done": position,
                                "levels_total": len(levels),
                                "done": done,
                                "total": total,
                            },
                        )

                payload = run_llm_experiment(
                    prepared,
                    fit.config.engine(),
                    adjudicator,
                    meter,
                    price_model=price_model,
                    price=price,
                    seed=seed,
                    sample_per_stratum=sample,
                    top_k=int(params["top_k"]),
                    on_progress=on_progress,
                )
                payload["config_id"] = fit.config.config_id
                payload["prompt_version"] = adjudicator.prompt_version
                # Withheld when too few grey-band calls answered: the row still
                # records the cost and the reason, with no accuracy to plot.
                routed = payload["strategies"].get("routed")
                session.add(
                    EvalRun(
                        sweep_id=row.id,
                        corruption_level=level,
                        strategy=str(StrategyName.PROBABILISTIC_LLM),
                        precision=routed["precision"] if routed else None,
                        recall=routed["recall"] if routed else None,
                        f1=routed["f1"] if routed else None,
                        false_positives=round(routed["false_positives"]) if routed else 0,
                        false_negatives=max(
                            0,
                            payload["population"]["expected_matches"]
                            - round(routed["true_positives"]),
                        )
                        if routed
                        else 0,
                        brier=None,
                        ece=None,
                        reliability_bins={},
                        blocking_recall=None,
                        detail=payload,
                    )
                )
                written += 1
                _progress(
                    session,
                    row,
                    {"levels_done": position + 1, "levels_total": len(levels), "done": 0, "total": 0},
                )
        finally:
            router.close()

        row.status = str(RunStatus.COMPLETED)
        row.finished_at = datetime.now(UTC)
        row.summary = {"seconds": round(time.perf_counter() - started, 1), "levels": written}
        session.commit()
    except Exception as exc:
        _fail(session, lab_id, exc)
        raise
    return {"lab_id": str(lab_id), "levels": written}


def _eval_row(cell: dict[str, Any], sweep_id: uuid.UUID) -> EvalRun:
    detail = dict(cell.get("detail") or {})
    return EvalRun(
        sweep_id=sweep_id,
        corruption_level=cell["corruption_level"],
        strategy=cell["strategy"],
        precision=cell["precision"],
        recall=cell["recall"],
        f1=cell["f1"],
        false_positives=cell["false_positives"],
        false_negatives=cell["false_negatives"],
        brier=cell["brier"],
        ece=cell["ece"],
        reliability_bins={"bins": cell.get("reliability_bins") or []},
        blocking_recall=detail.get("blocking_recall"),
        detail=detail,
    )


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def latest(
    session: Session,
    kind: LabKind,
    *,
    parent_id: uuid.UUID | None = None,
    completed: bool = False,
) -> LabSweep | None:
    stmt = select(LabSweep).where(LabSweep.kind == str(kind))
    if parent_id is not None:
        stmt = stmt.where(LabSweep.parent_id == parent_id)
    if completed:
        stmt = stmt.where(LabSweep.status == str(RunStatus.COMPLETED))
    return session.scalar(stmt.order_by(LabSweep.created_at.desc()).limit(1))


def effective_status(
    session: Session, row: LabSweep, *, now: datetime | None = None
) -> tuple[str, str | None]:
    """The row's status, corrected when the process running it died without saying so.

    A worker killed mid-sweep never reaches `_fail`, and the row would read as
    running forever. With a job, the job row knows better: a live experiment
    whose job is dead reads as failed, with the job's own error. Without one -
    run inline from the CLI - the only evidence is silence, so a row that has
    made no progress for `STALE_AFTER` reads as failed too. Progress is saved at
    least every few model calls and every sweep level, so half an hour of
    nothing is a dead process, not a slow one.
    """
    if row.status not in LIVE:
        return row.status, row.error
    if row.job_id is not None:
        job = session.get(Job, row.job_id)
        if job is not None and job.status in (str(JobStatus.DEAD), str(JobStatus.FAILED)):
            return str(RunStatus.FAILED), job.last_error or "the job running this experiment died"
    elif row.updated_at is not None:
        # Jobless and queued is no better: nothing will ever claim it.
        if (now or datetime.now(UTC)) - row.updated_at > STALE_AFTER:
            return str(RunStatus.FAILED), (
                "no progress for 30 minutes; the process running it has probably exited"
            )
    return row.status, row.error


def list_experiments(session: Session, limit: int = 20) -> list[LabSweep]:
    return list(
        session.scalars(select(LabSweep).order_by(LabSweep.created_at.desc()).limit(limit))
    )


def results(session: Session, settings: Settings, sweep_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Everything the Lab page draws, for one sweep and its latest LLM experiment.

    With no id, the newest *completed* sweep - the page should keep showing the
    last good curve while a new sweep runs, not go blank - plus whichever
    experiment is live, so the page can show its progress alongside.
    """
    from concordance.llm.pricing import PriceTable

    sweep = session.get(LabSweep, sweep_id) if sweep_id else latest(session, LabKind.SWEEP, completed=True)
    if sweep_id is not None and (sweep is None or sweep.kind != LabKind.SWEEP):
        raise NotFoundError(f"no sweep {sweep_id}")

    live = _live(session)
    price = PriceTable.load(settings.LLM_PRICE_TABLE).get(settings.LAB_PRICE_MODEL)
    out: dict[str, Any] = {
        "sweep": sweep,
        "llm_run": None,
        "live": live,
        "cells": [],
        "calibration": [],
        "llm": [],
        "price": {
            "model": settings.LAB_PRICE_MODEL,
            "prompt_per_million": price.prompt_per_million,
            "completion_per_million": price.completion_per_million,
            "placeholder": True,
        },
        "llm_enabled": settings.LLM_ENABLED,
    }
    if sweep is None:
        return out

    rows = session.scalars(
        select(EvalRun)
        .where(EvalRun.sweep_id == sweep.id)
        .order_by(EvalRun.corruption_level, EvalRun.strategy)
    )
    for row in rows:
        out["cells"].append(_cell(row))
        fit = (row.detail or {}).get("calibration_fit")
        if fit and row.strategy == StrategyName.PROBABILISTIC:
            for model, block in sorted(fit.items()):
                out["calibration"].append(_calibration(row.corruption_level, model, block))

    llm_run = latest(session, LabKind.LLM, parent_id=sweep.id, completed=True) or latest(
        session, LabKind.LLM, parent_id=sweep.id
    )
    out["llm_run"] = llm_run
    if llm_run is not None:
        for row in session.scalars(
            select(EvalRun).where(EvalRun.sweep_id == llm_run.id).order_by(EvalRun.corruption_level)
        ):
            out["llm"].append(_llm_level(row))
    return out


def _cell(row: EvalRun) -> dict[str, Any]:
    detail = row.detail or {}
    overall = detail.get("overall") or {}
    scenarios = [
        {
            "scenario": name,
            "n": tally.get("n", 0),
            "accuracy": tally.get("accuracy"),
            "precision": tally.get("precision"),
            "recall": tally.get("recall"),
            "f1": tally.get("f1"),
        }
        for name, tally in sorted((detail.get("by_scenario") or {}).items())
    ]
    return {
        "level": row.corruption_level,
        "strategy": row.strategy,
        "precision": row.precision,
        "recall": row.recall,
        "f1": row.f1,
        "accuracy": overall.get("accuracy"),
        "ambiguous_accuracy": overall.get("ambiguous_accuracy"),
        "wrong_provider": overall.get("wrong_provider"),
        "false_positives": row.false_positives,
        "false_negatives": row.false_negatives,
        "ece": row.ece,
        "brier": row.brier,
        "blocking_recall": row.blocking_recall,
        "grey_band_fraction": detail.get("grey_band_fraction"),
        "records": (detail.get("dataset") or {}).get("records") or overall.get("n"),
        "scenarios": scenarios,
    }


def _llm_level(row: EvalRun) -> dict[str, Any]:
    detail = row.detail or {}
    cost = detail.get("cost") or {}
    return {
        "level": row.corruption_level,
        "sample_per_stratum": detail.get("sample_per_stratum", 0),
        "population": detail.get("population") or {},
        "sample": detail.get("sample") or {},
        "strategies": detail.get("strategies") or {},
        "cost": {
            "price_model": cost.get("price_model", ""),
            "routed": cost.get("routed") or {},
            "everything": cost.get("everything") or {},
            "saving": cost.get("saving") or {},
            "tokens_per_call": cost.get("tokens_per_call") or {},
        },
        "notes": detail.get("notes") or [],
        "config_id": detail.get("config_id"),
    }


def _metrics(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "n": block.get("n", 0),
        "ece": block.get("ece"),
        "mce": block.get("mce"),
        "brier": block.get("brier"),
        "bins": block.get("bins") or [],
    }


def _calibration(level: float | None, model: str, block: dict[str, Any]) -> dict[str, Any]:
    thresholds = block.get("thresholds") or {}
    return {
        "level": level,
        "model": model,
        "before": _metrics(block.get("before") or {}),
        "after": _metrics(block.get("after") or {}),
        "t_auto_accept": thresholds.get("t_auto_accept"),
        "t_auto_reject": thresholds.get("t_auto_reject"),
        "target_precision": thresholds.get("target_precision"),
        "achieved_precision": thresholds.get("achieved_precision"),
        "grey_band_fraction": thresholds.get("grey_band_fraction"),
        "n_holdout": block.get("n_holdout"),
    }


__all__ = [
    "DEFAULT_LLM_LEVELS",
    "DEFAULT_LLM_SAMPLE",
    "SWEEP_STRATEGIES",
    "effective_status",
    "latest",
    "list_experiments",
    "request_llm",
    "request_sweep",
    "results",
    "run_llm",
    "run_sweep",
]
