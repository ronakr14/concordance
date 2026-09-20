"""The feedback loop against the real database: labels in, a proposed version out.

One small run, then reviewer verdicts written the way the review service
writes them - a `feedback_events` row carrying the comparison vector of the
candidate judged - labelled from ground truth. Every other result has its
runner-up rejected rather than its top candidate judged, so both classes are
present whatever scenario the slice happens to hold.

Everything created is deleted afterwards, and the activation history is put
back exactly as it was, so the config the rest of the suite scores with never
changes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SLICE = 120


@dataclass
class Loop:
    settings: Any
    run_id: uuid.UUID
    parent_id: uuid.UUID
    labels: int
    created_configs: list[uuid.UUID]
    created_activations: list[int]


@pytest.fixture(scope="module")
def loop(owner_url: str) -> Iterator[Loop]:
    from sqlalchemy import delete, select

    from concordance.config import Settings
    from concordance.db.enums import FeedbackLabel
    from concordance.db.models import (
        AuditLog,
        ConfigActivation,
        FeedbackEvent,
        GroundTruth,
        MatchCandidate,
        MatchResult,
        ReconciliationRun,
        ScoringConfig,
    )
    from concordance.db.repositories.configs import ConfigRepository
    from concordance.db.session import dispose_engine, session_scope
    from concordance.jobs.reconcile import RunRequest, ensure_scoring_config, reconcile

    dispose_engine()
    settings = Settings(
        DATABASE_URL=owner_url, DB_CONNECT_TIMEOUT=20, RETUNE_MIN_LABELS=40, AUDIT_RATE=0.05
    )
    with session_scope(settings) as session:
        before_activations = {a.id for a in session.scalars(select(ConfigActivation))}
        parent = ensure_scoring_config(session, settings)
        parent_id = parent.id
        report = reconcile(
            session, settings, RunRequest(strategy="probabilistic", limit=SLICE, show_progress=False)
        )
        run_id = report.run_id

        rows = session.execute(
            select(MatchResult, GroundTruth.expected_provider_id)
            .join(GroundTruth, GroundTruth.sanction_record_id == MatchResult.sanction_record_id)
            .where(MatchResult.run_id == run_id)
            .order_by(MatchResult.id)
        ).all()
        written = 0
        for i, (result, expected) in enumerate(rows):
            candidates = session.scalars(
                select(MatchCandidate)
                .where(MatchCandidate.match_result_id == result.id)
                .order_by(MatchCandidate.rank)
            ).all()
            if not candidates:
                continue
            judged = candidates[1] if i % 2 and len(candidates) > 1 else candidates[0]
            truly = expected is not None and judged.provider_id == expected
            session.add(
                FeedbackEvent(
                    match_result_id=result.id,
                    reviewer_id=None,
                    label=str(FeedbackLabel.TRUE_MATCH if truly else FeedbackLabel.FALSE_MATCH),
                    comparison_vector={
                        "provider_id": judged.provider_id,
                        "rank": judged.rank,
                        "field_levels": dict(judged.field_levels or {}),
                        "model": (result.explanation or {}).get("model"),
                        "run_id": str(run_id),
                    },
                )
            )
            written += 1
        session.commit()

    state = Loop(settings, run_id, parent_id, written, [], [])
    yield state

    with session_scope(settings) as session:
        results = select(MatchResult.id).where(MatchResult.run_id == run_id)
        session.execute(delete(FeedbackEvent).where(FeedbackEvent.match_result_id.in_(results)))
        added = [
            a.id for a in session.scalars(select(ConfigActivation))
            if a.id not in before_activations
        ]
        session.execute(delete(ConfigActivation).where(ConfigActivation.id.in_(added)))
        ids = [str(c) for c in state.created_configs]
        session.execute(delete(AuditLog).where(AuditLog.entity_id.in_(ids)))
        session.execute(delete(ScoringConfig).where(ScoringConfig.id.in_(state.created_configs)))
        session.execute(delete(ReconciliationRun).where(ReconciliationRun.id == run_id))
        assert ConfigRepository(session).active().id == parent_id  # type: ignore[union-attr]
    dispose_engine()


def test_every_verdict_becomes_a_label_with_its_evidence(loop: Loop) -> None:
    from concordance.db.session import session_scope
    from concordance.learning.service import collect_labels

    with session_scope(loop.settings) as session:
        labels, unusable = collect_labels(session, loop.run_id)
    mine = [x for x in labels if x.in_tally]
    assert unusable == 0
    assert len(mine) == loop.labels
    assert {x.label for x in mine} == {0, 1}, "the slice produced one class only"
    assert all(x.source in ("review", "audit", "voluntary") for x in mine)


def test_a_retune_proposes_a_new_version_and_does_not_activate_it(loop: Loop) -> None:
    from concordance.audit.service import Actor
    from concordance.db.session import session_scope
    from concordance.jobs.reconcile import ensure_scoring_config
    from concordance.learning.service import retune_active

    with session_scope(loop.settings) as session:
        done = retune_active(session, loop.settings, Actor.system(), run_id=loop.run_id)
        loop.created_configs.append(done.row.id)
        row = done.row
        assert row.parent_id == loop.parent_id
        assert row.fitted_from == "semi_supervised"
        assert row.version.endswith(".r1") or ".r" in row.version
        assert row.metrics["labels"]["total"] >= loop.labels
        assert row.metrics["tally_run_id"] == str(loop.run_id)
        assert set(row.metrics["holdout"]) == {"new", "parent"}
        session.commit()
        assert ensure_scoring_config(session, loop.settings).id == loop.parent_id


def test_activation_switches_what_runs_score_with_and_is_refused_twice(loop: Loop) -> None:
    from concordance.audit.service import Actor
    from concordance.db.session import session_scope
    from concordance.errors import ConflictError
    from concordance.jobs.reconcile import ensure_scoring_config
    from concordance.learning.service import activate_config, config_summaries

    assert loop.created_configs, "the retune test did not run first"
    new_id = loop.created_configs[0]
    with session_scope(loop.settings) as session:
        activate_config(session, Actor.system(), new_id, reason="integration test")
        session.commit()
        assert ensure_scoring_config(session, loop.settings).id == new_id
        with pytest.raises(ConflictError):
            activate_config(session, Actor.system(), new_id)
        rows = {r["id"]: r for r in config_summaries(session)}
        assert rows[new_id]["active"] is True
        assert rows[loop.parent_id]["active"] is False
        assert rows[new_id]["parent_version"] == rows[loop.parent_id]["version"]
        # Put the parent back so the rest of the suite scores with it.
        activate_config(session, Actor.system(), loop.parent_id, reason="integration test cleanup")
        session.commit()


def test_too_few_labels_is_refused_with_a_reason(loop: Loop) -> None:
    from concordance.audit.service import Actor
    from concordance.db.session import session_scope
    from concordance.errors import InvalidError
    from concordance.learning.service import retune_active

    strict = loop.settings.model_copy(update={"RETUNE_MIN_LABELS": 100_000})
    with session_scope(strict) as session, pytest.raises(InvalidError) as refused:
        retune_active(session, strict, Actor.system(), run_id=loop.run_id)
    assert refused.value.code == "not_enough_labels"
