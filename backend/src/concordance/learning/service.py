"""The feedback loop against the database: labels in, config versions out.

`retune.py` is the arithmetic and knows nothing about tables. This module
reads what it needs - the active config, a run's pair tally, every reviewer
verdict - writes the new version, and records who asked for it. Activation is
its own function and its own audit row: a retune proposes, a person decides.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from concordance.audit import service as audit
from concordance.audit.service import Actor
from concordance.config import Settings
from concordance.db.enums import Decision, FeedbackLabel, FittedFrom, RunStatus
from concordance.db.models import (
    FeedbackEvent,
    MatchCandidate,
    MatchResult,
    ReconciliationRun,
    RunPattern,
)
from concordance.db.models import ScoringConfig as ScoringConfigRow
from concordance.db.repositories.configs import ConfigRepository
from concordance.errors import ConflictError, InvalidError, NotFoundError
from concordance.learning.retune import (
    Label,
    NotEnoughLabelsError,
    RetuneResult,
    label_counts,
    retune,
    vector_from_levels,
)
from concordance.logging_setup import get_logger
from concordance.matching.comparators import ComparisonVector, ModelKind
from concordance.matching.fellegi_sunter import PatternCounts
from concordance.matching.scoring_config import ScoringConfig

log = get_logger("learning.service")

_GENERATION = re.compile(r"^(?P<root>.+)\.r(?P<n>\d+)$")


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------


def collect_labels(session: Session, tally_run_id: uuid.UUID | None) -> tuple[list[Label], int]:
    """Every usable reviewer verdict, one per result; and how many were unusable.

    The newest verdict on a result wins. The source is what decides a label's
    weight: an audited auto-reject stands for `1 / audit_rate` records like it;
    a reviewed match or grey-band result stands for itself; an auto-reject a
    reviewer opened on their own has no known inclusion probability and is
    marked `voluntary` (see `retune.py` for what that changes).
    """
    rows = session.execute(
        select(FeedbackEvent, MatchResult, ReconciliationRun.audit_rate)
        .join(MatchResult, MatchResult.id == FeedbackEvent.match_result_id)
        .join(ReconciliationRun, ReconciliationRun.id == MatchResult.run_id)
        .order_by(FeedbackEvent.created_at, FeedbackEvent.id)
    ).all()
    newest: dict[uuid.UUID, tuple[FeedbackEvent, MatchResult, float | None]] = {}
    for event, result, rate in rows:
        newest[result.id] = (event, result, rate)

    labels: list[Label] = []
    unusable = 0
    for event, result, rate in newest.values():
        seen = event.comparison_vector or {}
        recorded = seen.get("model") or (result.explanation or {}).get("model")
        # A cross-type pair was scored as an organization whatever the record
        # was filed as, so the other model is tried before giving up.
        found = _vector_for(dict(seen.get("field_levels") or {}), recorded)
        if found is None:
            unusable += 1
            continue
        kind, vector = found
        if result.audit_sampled and rate:
            source, weight = "audit", 1.0 / rate
        elif result.decision == Decision.NO_MATCH:
            source, weight = "voluntary", 1.0
        else:
            source, weight = "review", 1.0
        labels.append(
            Label(
                key=str(result.id),
                kind=kind,
                vector=vector,
                label=1 if event.label == FeedbackLabel.TRUE_MATCH else 0,
                weight=weight,
                source=source,
                in_tally=tally_run_id is not None and result.run_id == tally_run_id,
            )
        )
    return labels, unusable


def _vector_for(
    levels: dict[str, Any], preferred: str | None
) -> tuple[ModelKind, ComparisonVector] | None:
    """A stored set of level names back to (model, vector), preferred model first."""
    kinds = [ModelKind(preferred)] if preferred in {k.value for k in ModelKind} else []
    kinds += [k for k in ModelKind if k not in kinds]
    for kind in kinds:
        vector = vector_from_levels(kind, levels)
        if vector is not None:
            return kind, vector
    return None


def run_population(
    session: Session, run_id: uuid.UUID
) -> list[list[tuple[ModelKind, ComparisonVector]]]:
    """Every result of a run as its stored candidates' (model, vector) pairs.

    What a retune places its thresholds on: each record rescored under the new
    config, as it would be on the next run. Only the top-k candidates were
    stored, so a candidate the new model would lift from below the cut is not
    seen - at the default k that changes the best candidate of almost no record.
    """
    rows = session.execute(
        select(
            MatchCandidate.match_result_id,
            MatchCandidate.field_levels,
            MatchResult.explanation,
        )
        .join(MatchResult, MatchResult.id == MatchCandidate.match_result_id)
        .where(MatchResult.run_id == run_id)
        .order_by(MatchCandidate.match_result_id, MatchCandidate.rank)
    )
    grouped: dict[uuid.UUID, list[tuple[ModelKind, ComparisonVector]]] = {}
    for result_id, levels, explanation in rows:
        found = _vector_for(dict(levels or {}), (explanation or {}).get("model"))
        if found is not None:
            grouped.setdefault(result_id, []).append(found)
    return list(grouped.values())


# --------------------------------------------------------------------------
# retune
# --------------------------------------------------------------------------


@dataclass
class Retuned:
    row: ScoringConfigRow
    result: RetuneResult
    activated: bool


def tally_run(session: Session, run_id: uuid.UUID | None = None) -> ReconciliationRun:
    """The run whose pair tally a retune fits on: the one named, or the newest with one."""
    if run_id is not None:
        run = session.get(ReconciliationRun, run_id)
        if run is None:
            raise NotFoundError(f"no run {run_id}")
        if not session.scalar(select(RunPattern.run_id).where(RunPattern.run_id == run_id).limit(1)):
            raise ConflictError(
                "that run has no pair tally; runs reconciled before the feedback loop do not",
                code="no_pair_tally",
            )
        return run
    run = session.scalar(
        select(ReconciliationRun)
        .where(
            ReconciliationRun.status == RunStatus.COMPLETED,
            ReconciliationRun.id.in_(select(RunPattern.run_id).distinct()),
        )
        .order_by(ReconciliationRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        raise ConflictError(
            "no completed run has a pair tally yet; reconcile once, then retune",
            code="no_pair_tally",
        )
    return run


def next_version(session: Session, parent_version: str) -> str:
    """`<root>.r<n>`: the root EM fit's version, and the generation since it."""
    found = _GENERATION.match(parent_version)
    root, n = (found["root"], int(found["n"]) + 1) if found else (parent_version, 1)
    repo = ConfigRepository(session)
    while repo.by_version(f"{root}.r{n}") is not None:
        n += 1
    return f"{root}.r{n}"


def retune_active(
    session: Session,
    settings: Settings,
    actor: Actor,
    *,
    run_id: uuid.UUID | None = None,
    activate: bool = False,
) -> Retuned:
    """Retune the active config on every label so far. The caller commits."""
    configs = ConfigRepository(session)
    parent_row = configs.active()
    if parent_row is None:
        raise ConflictError("no active scoring config to retune; run a reconciliation first")
    parent = ScoringConfig.from_dict(parent_row.params)
    run = tally_run(session, run_id)
    patterns = {
        kind: PatternCounts(kind, tuple(v for v, _ in rows), tuple(n for _, n in rows))
        for kind, rows in configs.patterns(run.id).items()
    }
    labels, unusable = collect_labels(session, run.id)
    version = next_version(session, parent_row.version)
    try:
        result = retune(
            parent,
            patterns,
            labels,
            config_id=version,
            seed=settings.RANDOM_SEED,
            target_precision=settings.TARGET_PRECISION,
            min_labels=settings.RETUNE_MIN_LABELS,
            label_noise=settings.REVIEWER_ERROR_RATE,
            population=run_population(session, run.id),
        )
    except NotEnoughLabelsError as exc:
        raise InvalidError(
            str(exc),
            code="not_enough_labels",
            details={"labels": str(exc.counts), "minimum": str(settings.RETUNE_MIN_LABELS)},
        ) from exc

    config = result.config
    individual = config.bundles[ModelKind.INDIVIDUAL].thresholds
    metrics = {
        **result.metrics,
        "tally_run_id": str(run.id),
        "unusable_labels": unusable,
    }
    row = ScoringConfigRow(
        version=version,
        params=config.as_dict(),
        t_auto_accept=float(individual.t_auto_accept),
        t_auto_reject=float(individual.t_auto_reject),
        calibrator={str(k): b.calibrator.as_dict() for k, b in config.bundles.items()},
        fitted_from=str(FittedFrom.SEMI_SUPERVISED),
        notes="; ".join(result.notes) or None,
        parent_id=parent_row.id,
        metrics=metrics,
        created_by=actor.user_id,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        actor,
        "config.retuned",
        entity_type="scoring_config",
        entity_id=row.id,
        after={
            "version": version,
            "parent": parent_row.version,
            "labels": label_counts(labels),
            "holdout": metrics["holdout"],
            "tally_run_id": str(run.id),
        },
    )
    log.info(
        "retune.done",
        version=version,
        parent=parent_row.version,
        labels=len(labels),
        improved=result.improved,
    )
    if activate:
        activate_config(session, actor, row.id, reason="activated on retune")
    return Retuned(row=row, result=result, activated=activate)


def activate_config(
    session: Session, actor: Actor, config_id: uuid.UUID, *, reason: str | None = None
) -> ScoringConfigRow:
    """Make a config the one new runs score with. The caller commits."""
    configs = ConfigRepository(session)
    row = configs.get(config_id)
    if row is None:
        raise NotFoundError(f"no scoring config {config_id}")
    previous = configs.active()
    if previous is not None and previous.id == row.id:
        raise ConflictError(
            f"{row.version} is already the active config", code="already_active"
        )
    configs.activate(row, actor_id=actor.user_id, reason=reason)
    audit.record(
        session,
        actor,
        "config.activated",
        entity_type="scoring_config",
        entity_id=row.id,
        before={"active": previous.version if previous else None},
        after={"active": row.version, "reason": reason},
    )
    return row


def config_summaries(session: Session) -> list[dict[str, Any]]:
    """Every version with its lineage, measurements, run count and active flag."""
    configs = ConfigRepository(session)
    active = configs.active()
    runs = configs.run_counts()
    rows = configs.all()
    versions = {r.id: r.version for r in rows}
    out: list[dict[str, Any]] = []
    for r in rows:
        count, latest = runs.get(r.id, (0, None))
        out.append(
            {
                "id": r.id,
                "version": r.version,
                "fitted_from": r.fitted_from,
                "fitted_at": r.fitted_at,
                "parent_id": r.parent_id,
                "parent_version": versions.get(r.parent_id) if r.parent_id else None,
                "t_auto_accept": r.t_auto_accept,
                "t_auto_reject": r.t_auto_reject,
                "metrics": dict(r.metrics or {}),
                "notes": r.notes,
                "created_by": r.created_by,
                "active": active is not None and active.id == r.id,
                "runs": count,
                "latest_run_at": latest,
            }
        )
    return out


__all__ = [
    "Retuned",
    "activate_config",
    "collect_labels",
    "config_summaries",
    "next_version",
    "retune_active",
    "run_population",
    "tally_run",
]
