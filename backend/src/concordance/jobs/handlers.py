"""What each job kind actually does.

Nine kinds, and the split between them is the split between work that decides
something about production data and work that measures the engine:

- `reconcile` - score the sanction records against the provider master and
  persist the decisions. The only kind that writes `match_results`.
- `expire_cases` - the scheduled Q3 transition. Idempotent, system-actored.
- `eval` - measure a strategy against ground truth and record an `eval_runs`
  row. Reads the Parquet dataset, because ground truth is a property of the
  synthetic corpus rather than of production.
- `refit` - refit the Fellegi-Sunter models from scratch on a dataset (EM, then
  calibration against its ground truth) and register the result as a new
  immutable `scoring_configs` row. It never edits the config a finished run
  points at; a refit is a new version.
- `retune` - the feedback loop: retune the *active* config on reviewer labels
  (semi-supervised EM over a run's pair tally, then recalibration). Proposes a
  version; activating it is a separate decision. See `learning.service`.
- `sweep` - the robustness curve across corruption levels, written to a file.
- `lab_sweep` / `lab_llm` / `lab_feedback` - the Lab's experiments, written to
  `lab_sweeps` (and `eval_runs`) for the pages to read. See `lab.service`.

Every handler takes the session the worker opened and returns a small summary
dict. Most of them leave the transaction to the worker, so a handler that
half-writes leaves nothing behind. `reconcile` is the exception and says so in
its own module: a run of five thousand records commits per chunk, because a run
whose progress is invisible until it ends is not a run anyone can watch, and a
failure at record four thousand should keep the four thousand decisions it
already made.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from concordance.audit import service as audit
from concordance.audit.service import Actor
from concordance.cases.lifecycle import expire_cases as _expire_cases
from concordance.config import Settings
from concordance.db.repositories.evaluation import EvalRepository
from concordance.jobs.reconcile import RunRequest, import_scoring_config, reconcile
from concordance.jobs.registry import register
from concordance.logging_setup import get_logger
from concordance.matching.strategies import ALL_STRATEGIES, StrategyName, build_strategy

log = get_logger("jobs.handlers")


@register("reconcile")
def handle_reconcile(
    session: Session, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    """One reconciliation run, end to end."""
    return reconcile(session, settings, RunRequest.from_payload(payload)).as_dict()


@register("expire_cases")
def handle_expire_cases(
    session: Session,
    settings: Settings,  # noqa: ARG001 - every handler takes the same three
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Close the window on cases whose end date has passed (Q3)."""
    from datetime import date

    today = payload.get("today")
    return _expire_cases(session, today=date.fromisoformat(today) if today else None).as_dict()


@register("eval")
def handle_eval(session: Session, settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    """Measure one strategy against ground truth and record the row."""
    from concordance.eval.harness import evaluate, write_report
    from concordance.eval.pairs import prepare
    from concordance.matching.scoring_config import ScoringConfig

    dataset = Path(payload.get("dataset") or settings.generated_dir)
    strategy_name = str(payload.get("strategy") or StrategyName.PROBABILISTIC)
    prepared = prepare(
        dataset,
        max_candidates=int(payload.get("max_candidates") or settings.MAX_CANDIDATES_PER_RECORD),
        limit=payload.get("limit"),
        show_progress=False,
    )

    config_row = _config_row(session, settings, payload.get("config_version"))
    engine = ScoringConfig.from_dict(config_row.params).engine()
    report = evaluate(
        prepared,
        build_strategy(strategy_name, engine, None),
        config_id=config_row.version,
        seed=int(payload.get("seed") or settings.RANDOM_SEED),
        show_progress=False,
    )

    row = report.as_dict()
    EvalRepository(session).record_eval(
        run_id=_as_uuid(payload.get("run_id")),
        corruption_level=row["corruption_level"],
        strategy=row["strategy"],
        precision=row["precision"],
        recall=row["recall"],
        f1=row["f1"],
        false_positives=row["false_positives"],
        false_negatives=row["false_negatives"],
        brier=row["brier"],
        ece=row["ece"],
        reliability_bins={"bins": row["reliability_bins"]},
        blocking_recall=report.blocking_recall,
        scoring_config_id=config_row.id,
    )

    written = ""
    if payload.get("report", True):
        settings.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        written = str(
            write_report(report, settings.REPORTS_DIR / f"eval_{strategy_name}_job.html")
        )
    return {
        "strategy": row["strategy"],
        "precision": row["precision"],
        "recall": row["recall"],
        "f1": row["f1"],
        "config": config_row.version,
        "report": written,
    }


@register("refit")
def handle_refit(session: Session, settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    """Refit the models from scratch and register the result as a new immutable config."""
    from concordance.eval.fitting import fit_config
    from concordance.eval.pairs import prepare

    dataset = Path(payload.get("dataset") or settings.generated_dir)
    seed = int(payload.get("seed") or settings.RANDOM_SEED)
    prepared = prepare(
        dataset,
        max_candidates=int(payload.get("max_candidates") or settings.MAX_CANDIDATES_PER_RECORD),
        show_progress=False,
    )
    fitted = fit_config(
        prepared,
        seed=seed,
        target_precision=float(payload.get("target_precision") or settings.TARGET_PRECISION),
    )
    if payload.get("write_file", True):
        fitted.config.write(settings.DATA_DIR / "configs" / f"{fitted.config.config_id}.json")
    row = import_scoring_config(session, fitted.config, notes="refit job")
    # A new config changes what every later run decides, so it is recorded
    # like any other change of state - by the system, since a job did it.
    audit.record(
        session,
        Actor.system(),
        "config.refitted",
        entity_type="scoring_config",
        entity_id=row.id,
        after={
            "version": row.version,
            "t_auto_accept": row.t_auto_accept,
            "t_auto_reject": row.t_auto_reject,
            "fitted_from": row.fitted_from,
            "requested_by": payload.get("triggered_by"),
        },
    )
    return {
        "config": row.version,
        "scoring_config_id": str(row.id),
        "t_auto_accept": row.t_auto_accept,
        "t_auto_reject": row.t_auto_reject,
    }


@register("retune")
def handle_retune(session: Session, settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    """Retune the active config on reviewer labels. Proposes; does not activate unless asked."""
    from concordance.learning.service import retune_active

    run_id = payload.get("run_id")
    retuned = retune_active(
        session,
        settings,
        Actor.system(),
        run_id=uuid.UUID(str(run_id)) if run_id else None,
        activate=bool(payload.get("activate", False)),
    )
    return {
        "config": retuned.row.version,
        "scoring_config_id": str(retuned.row.id),
        "improved": retuned.result.improved,
        "recommended": retuned.result.recommended,
        "verdict": retuned.result.verdict,
        "activated": retuned.activated,
    }


@register("sweep")
def handle_sweep(
    session: Session,  # noqa: ARG001 - the sweep writes files, not rows
    settings: Settings,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """The robustness curve. Long-running by nature, which is why it is a job."""
    from concordance.eval.sweep import sweep, write_sweep

    levels = tuple(float(x) for x in payload.get("levels") or ()) or None
    strategies = tuple(StrategyName(s) for s in payload.get("strategies") or ()) or ALL_STRATEGIES
    kwargs: dict[str, Any] = {
        "root": Path(payload.get("root") or settings.DATA_DIR / "sweep"),
        "seed": int(payload.get("seed") or settings.RANDOM_SEED),
        "strategies": strategies,
        "providers": int(payload.get("providers") or 50_000),
        "sanctions": int(payload.get("sanctions") or 5_000),
        "workers": int(payload.get("workers") or 1),
    }
    if levels:
        kwargs["levels"] = levels
    result = sweep(**kwargs)
    settings.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = write_sweep(result, settings.REPORTS_DIR / "sweep_job.json")
    return {"cells": len(result.cells), "report": str(path)}


@register("lab_sweep")
def handle_lab_sweep(
    session: Session, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    """A Lab sweep: the robustness curve, written to `eval_runs` for the page."""
    from concordance.lab.service import run_sweep

    return run_sweep(session, settings, uuid.UUID(str(payload["lab_id"])))


@register("lab_llm")
def handle_lab_llm(
    session: Session, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    """A Lab LLM experiment: routed versus LLM-on-everything, on a sample."""
    from concordance.lab.service import run_llm

    return run_llm(session, settings, uuid.UUID(str(payload["lab_id"])))


@register("lab_feedback")
def handle_lab_feedback(
    session: Session, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    from concordance.lab.service import run_feedback

    return run_feedback(session, settings, uuid.UUID(str(payload["lab_id"])))


def _config_row(session: Session, settings: Settings, version: str | None) -> Any:
    from concordance.jobs.reconcile import ensure_scoring_config

    return ensure_scoring_config(session, settings, version)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


__all__ = [
    "handle_eval",
    "handle_expire_cases",
    "handle_lab_feedback",
    "handle_lab_llm",
    "handle_lab_sweep",
    "handle_reconcile",
    "handle_refit",
    "handle_retune",
    "handle_sweep",
]
