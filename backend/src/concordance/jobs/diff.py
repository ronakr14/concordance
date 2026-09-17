"""Diff two runs, and say what changed the answers.

A list of records that decided differently is interesting; a list of records
that decided differently *next to the config delta that explains it* is
actionable. So the report has two halves:

- **What moved.** Records grouped as unchanged, decision changed, confidence
  changed beyond a threshold, new (decided in B and not in A) and removed
  (decided in A and not in B). Confidence gets a threshold rather than exact
  comparison because a refit moves every posterior slightly and a list of five
  thousand "changed" records hides the twelve that matter.
- **Why.** The scoring config, engine version, prompt version, strategy and the
  two snapshot hashes, each reported as `a -> b` when they differ. If the
  configs differ the thresholds are compared too, since a threshold move is the
  most common single cause of a decision flip.

The output is a dataclass with `as_dict()` rather than printed text, because
the Stage 9 lab renders the same structure in the browser. The CLI formats it;
nothing here knows what a terminal is.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from concordance.db.models import MatchResult, ReconciliationRun
from concordance.db.models import SanctionRecord as SanctionRecordRow
from concordance.db.models import ScoringConfig as ScoringConfigRow
from concordance.logging_setup import get_logger

log = get_logger("jobs.diff")

#: A confidence move smaller than this is noise from a refit, not a finding.
DEFAULT_CONFIDENCE_DELTA = 0.05


@dataclass(frozen=True, slots=True)
class Decided:
    """One run's answer about one record."""

    decision: str
    provider_id: str | None
    confidence: float
    route: str


@dataclass
class ChangedRecord:
    record_id: str
    before: Decided
    after: Decided

    @property
    def decision_changed(self) -> bool:
        return (
            self.before.decision != self.after.decision
            or self.before.provider_id != self.after.provider_id
        )

    @property
    def confidence_delta(self) -> float:
        return self.after.confidence - self.before.confidence

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "decision_before": self.before.decision,
            "decision_after": self.after.decision,
            "provider_before": self.before.provider_id,
            "provider_after": self.after.provider_id,
            "confidence_before": round(self.before.confidence, 6),
            "confidence_after": round(self.after.confidence, 6),
            "confidence_delta": round(self.confidence_delta, 6),
            "route_before": self.before.route,
            "route_after": self.after.route,
        }


@dataclass
class ConfigDelta:
    """What differs between the two runs' provenance. Empty means nothing did."""

    changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)

    def note(self, name: str, before: Any, after: Any) -> None:
        if before != after:
            self.changes[name] = (before, after)

    def as_dict(self) -> dict[str, Any]:
        return {k: {"before": b, "after": a} for k, (b, a) in sorted(self.changes.items())}

    def lines(self) -> list[str]:
        return [f"  {name}: {before!r} -> {after!r}" for name, (before, after) in sorted(self.changes.items())]


@dataclass
class DiffReport:
    run_a: uuid.UUID
    run_b: uuid.UUID
    confidence_delta: float = DEFAULT_CONFIDENCE_DELTA
    unchanged: int = 0
    changed_decision: list[ChangedRecord] = field(default_factory=list)
    changed_confidence: list[ChangedRecord] = field(default_factory=list)
    new_records: list[str] = field(default_factory=list)
    removed_records: list[str] = field(default_factory=list)
    config: ConfigDelta = field(default_factory=ConfigDelta)

    def as_dict(self, detail: int = 200) -> dict[str, Any]:
        return {
            "run_a": str(self.run_a),
            "run_b": str(self.run_b),
            "confidence_threshold": self.confidence_delta,
            "counts": {
                "unchanged": self.unchanged,
                "changed_decision": len(self.changed_decision),
                "changed_confidence": len(self.changed_confidence),
                "new": len(self.new_records),
                "removed": len(self.removed_records),
            },
            "config_delta": self.config.as_dict(),
            "changed_decision": [c.as_dict() for c in self.changed_decision[:detail]],
            "changed_confidence": [c.as_dict() for c in self.changed_confidence[:detail]],
            "new": self.new_records[:detail],
            "removed": self.removed_records[:detail],
        }

    def lines(self, sample: int = 15) -> list[str]:
        out = [
            f"diff {self.run_a} -> {self.run_b}",
            f"  unchanged            {self.unchanged}",
            f"  changed decision     {len(self.changed_decision)}",
            f"  changed confidence   {len(self.changed_confidence)}  (> {self.confidence_delta})",
            f"  new / removed        {len(self.new_records)} / {len(self.removed_records)}",
        ]
        out.append("  config delta:" if self.config.changes else "  config delta: none")
        out.extend(self.config.lines())
        if self.changed_decision:
            out.append("  decisions that changed:")
            for change in self.changed_decision[:sample]:
                out.append(
                    f"    {change.record_id}  {change.before.decision}"
                    f"({change.before.provider_id or '-'}) -> {change.after.decision}"
                    f"({change.after.provider_id or '-'})"
                    f"  conf {change.before.confidence:.4f} -> {change.after.confidence:.4f}"
                )
        return out


def diff_runs(
    session: Session,
    run_a: uuid.UUID,
    run_b: uuid.UUID,
    *,
    confidence_delta: float = DEFAULT_CONFIDENCE_DELTA,
) -> DiffReport:
    """Compare two runs record by record, and report the provenance that differs."""
    first = _require_run(session, run_a)
    second = _require_run(session, run_b)

    left = _decisions(session, run_a)
    right = _decisions(session, run_b)
    report = DiffReport(run_a=run_a, run_b=run_b, confidence_delta=confidence_delta)

    for record_id, before in left.items():
        after = right.get(record_id)
        if after is None:
            report.removed_records.append(record_id)
            continue
        change = ChangedRecord(record_id=record_id, before=before, after=after)
        if change.decision_changed:
            report.changed_decision.append(change)
        elif abs(change.confidence_delta) > confidence_delta:
            report.changed_confidence.append(change)
        else:
            report.unchanged += 1

    report.new_records = sorted(set(right) - set(left))
    report.removed_records.sort()
    report.config = _config_delta(session, first, second)

    log.info(
        "diff.done",
        run_a=str(run_a),
        run_b=str(run_b),
        changed_decision=len(report.changed_decision),
        changed_confidence=len(report.changed_confidence),
    )
    return report


def _require_run(session: Session, run_id: uuid.UUID) -> ReconciliationRun:
    run = session.get(ReconciliationRun, run_id)
    if run is None:
        raise LookupError(f"no run {run_id}")
    return run


def _decisions(session: Session, run_id: uuid.UUID) -> dict[str, Decided]:
    rows = session.execute(
        select(
            SanctionRecordRow.record_id,
            MatchResult.decision,
            MatchResult.chosen_provider_id,
            MatchResult.calibrated_confidence,
            MatchResult.route,
        )
        .join(MatchResult, MatchResult.sanction_record_id == SanctionRecordRow.id)
        .where(MatchResult.run_id == run_id)
        .order_by(SanctionRecordRow.record_id)
    )
    return {
        record_id: Decided(
            decision=decision,
            provider_id=provider_id,
            confidence=float(confidence or 0.0),
            route=route,
        )
        for record_id, decision, provider_id, confidence, route in rows
    }


def _config_delta(
    session: Session, first: ReconciliationRun, second: ReconciliationRun
) -> ConfigDelta:
    """The provenance that differs. This is the answer to why a decision moved."""
    delta = ConfigDelta()
    delta.note("engine_version", first.engine_version, second.engine_version)
    delta.note("prompt_version", first.prompt_version, second.prompt_version)
    delta.note("strategy", first.strategy, second.strategy)
    delta.note(
        "provider_snapshot_hash", first.provider_snapshot_hash, second.provider_snapshot_hash
    )
    delta.note(
        "sanction_snapshot_hash", first.sanction_snapshot_hash, second.sanction_snapshot_hash
    )

    left = _config(session, first.scoring_config_id)
    right = _config(session, second.scoring_config_id)
    delta.note(
        "scoring_config",
        left.version if left else None,
        right.version if right else None,
    )
    if left is not None and right is not None and left.id != right.id:
        # A threshold move is the single most common cause of a decision flip,
        # so it is named rather than left inside "the config changed".
        delta.note("t_auto_accept", left.t_auto_accept, right.t_auto_accept)
        delta.note("t_auto_reject", left.t_auto_reject, right.t_auto_reject)

    for key in sorted(set(first.request or {}) | set(second.request or {})):
        delta.note(f"request.{key}", (first.request or {}).get(key), (second.request or {}).get(key))
    return delta


def _config(session: Session, config_id: uuid.UUID | None) -> ScoringConfigRow | None:
    return session.get(ScoringConfigRow, config_id) if config_id else None


__all__ = [
    "DEFAULT_CONFIDENCE_DELTA",
    "ChangedRecord",
    "ConfigDelta",
    "Decided",
    "DiffReport",
    "diff_runs",
]
