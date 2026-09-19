"""Scoring configs: versions, lineage, activation, and the pair tallies retuning fits on.

A config row is never edited. What changes over time is which config is
*active*, and that is a row in `config_activations` - so "which config did the
system use on the 3rd, and who switched it" is one query, and a retune written
tonight does not score tomorrow's run until somebody decides it should.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from concordance.db.models import (
    ConfigActivation,
    ReconciliationRun,
    RunPattern,
    ScoringConfig,
)
from concordance.matching.comparators import ModelKind

#: Rows per INSERT when writing a run's pattern tally.
PATTERN_BATCH = 1_000


class ConfigRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- configs ----------------------------------------------------------
    def get(self, config_id: uuid.UUID) -> ScoringConfig | None:
        return self.session.get(ScoringConfig, config_id)

    def by_version(self, version: str) -> ScoringConfig | None:
        return self.session.scalar(select(ScoringConfig).where(ScoringConfig.version == version))

    def all(self) -> list[ScoringConfig]:
        """Every version, newest first."""
        return list(
            self.session.scalars(select(ScoringConfig).order_by(ScoringConfig.fitted_at.desc()))
        )

    def children(self, config_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count()).select_from(ScoringConfig).where(
                    ScoringConfig.parent_id == config_id
                )
            )
            or 0
        )

    # -- activation -------------------------------------------------------
    def active(self) -> ScoringConfig | None:
        """The config new runs score with: the newest activation's."""
        return self.session.scalar(
            select(ScoringConfig)
            .join(ConfigActivation, ConfigActivation.scoring_config_id == ScoringConfig.id)
            .order_by(ConfigActivation.created_at.desc(), ConfigActivation.id.desc())
            .limit(1)
        )

    def activate(
        self, config: ScoringConfig, *, actor_id: uuid.UUID | None, reason: str | None
    ) -> ConfigActivation:
        row = ConfigActivation(scoring_config_id=config.id, activated_by=actor_id, reason=reason)
        self.session.add(row)
        self.session.flush()
        return row

    def activations(self, limit: int = 50) -> list[ConfigActivation]:
        return list(
            self.session.scalars(
                select(ConfigActivation)
                .order_by(ConfigActivation.created_at.desc(), ConfigActivation.id.desc())
                .limit(limit)
            )
        )

    def run_counts(self) -> dict[uuid.UUID, tuple[int, Any]]:
        """Runs per config, with the newest run's start time - "which config ran what"."""
        rows = self.session.execute(
            select(
                ReconciliationRun.scoring_config_id,
                func.count(),
                func.max(ReconciliationRun.created_at),
            )
            .where(ReconciliationRun.scoring_config_id.is_not(None))
            .group_by(ReconciliationRun.scoring_config_id)
        )
        return {config_id: (int(n), latest) for config_id, n, latest in rows}

    # -- pattern tallies --------------------------------------------------
    def write_patterns(
        self, run_id: uuid.UUID, tally: Counter[tuple[ModelKind, tuple[int, ...]]]
    ) -> int:
        """Store a run's candidate-pair tally. Returns the number of distinct vectors."""
        rows = [
            {"run_id": run_id, "kind": str(kind), "pattern": list(vector), "n": n}
            for (kind, vector), n in sorted(tally.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))
        ]
        for start in range(0, len(rows), PATTERN_BATCH):
            self.session.execute(insert(RunPattern), rows[start : start + PATTERN_BATCH])
        return len(rows)

    def patterns(self, run_id: uuid.UUID) -> dict[ModelKind, list[tuple[tuple[int, ...], int]]]:
        """A run's tally as `{kind: [(vector, count), ...]}`, in a stable order."""
        out: dict[ModelKind, list[tuple[tuple[int, ...], int]]] = {}
        query = (
            select(RunPattern.kind, RunPattern.pattern, RunPattern.n)
            .where(RunPattern.run_id == run_id)
            .order_by(RunPattern.kind, RunPattern.pattern)
        )
        for kind, pattern, n in self.session.execute(query):
            out.setdefault(ModelKind(kind), []).append((tuple(pattern), int(n)))
        return out

    def runs_with_patterns(self, run_ids: Iterable[uuid.UUID] | None = None) -> list[uuid.UUID]:
        query = select(RunPattern.run_id).distinct()
        if run_ids is not None:
            query = query.where(RunPattern.run_id.in_(list(run_ids)))
        return [r for (r,) in self.session.execute(query)]


__all__ = ["ConfigRepository"]
