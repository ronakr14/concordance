"""Runs, replay, diff, supersede and expiry - against the real database.

The fixture does the expensive part once: reconcile a slice of the sanction
file, open a case on one of the matches it found, then reconcile the same slice
again under a scoring config whose accept threshold has been raised so far that
every match becomes ambiguous. That second run is the interesting one, because
it is exactly the shape of the event Q5 is about - a config change that
disagrees with a decision a human already acted on.

Everything the fixture created is deleted afterwards, including the runs (whose
results and candidates cascade), the case, the audit rows and the throwaway
config. The dataset itself is never touched: no test here writes a provider or a
sanction record, so the snapshot hashes stay stable and replay keeps working.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: Enough records to contain matches and non-matches, few enough that two runs
#: plus a replay stay inside a coffee break against a hosted database.
SLICE = 40


@dataclass
class Fixture:
    settings: Any
    run_a: uuid.UUID
    run_b: uuid.UUID
    case_id: uuid.UUID
    result_a_id: uuid.UUID
    config_b_id: uuid.UUID
    sanction_record_id: uuid.UUID


@pytest.fixture(scope="module")
def settings(owner_url: str) -> Iterator[Any]:
    from concordance.config import Settings
    from concordance.db.session import dispose_engine

    dispose_engine()
    yield Settings(DATABASE_URL=owner_url, DB_CONNECT_TIMEOUT=20)
    dispose_engine()


@pytest.fixture(scope="module")
def scenario(settings: Any) -> Iterator[Fixture]:
    from sqlalchemy import delete, select

    from concordance.cases.lifecycle import open_case
    from concordance.db.enums import Decision
    from concordance.db.enums import Route as DbRoute
    from concordance.db.models import AuditLog, Case, MatchResult, ReconciliationRun
    from concordance.db.models import ScoringConfig as ScoringConfigRow
    from concordance.db.session import session_scope
    from concordance.jobs.reconcile import RunRequest, ensure_scoring_config, reconcile

    with session_scope(settings) as session:
        base_config = ensure_scoring_config(session, settings)
        base_version = base_config.version

        # The baseline run names its config explicitly. Letting it default would
        # pick the newest config in the table, which is the strict one this
        # fixture is about to create - and then both runs would score
        # identically and every assertion below would pass for the wrong reason.
        first = reconcile(
            session,
            settings,
            RunRequest(
                strategy="probabilistic",
                limit=SLICE,
                config_version=base_version,
                show_progress=False,
            ),
        )

        strict = _strict_copy(base_config)
        session.add(strict)
        session.commit()
        config_b_id = strict.id
        config_b_version = strict.version

        # A probabilistic match, not a deterministic one: an exact NPI agreement
        # is a match whatever the thresholds say, so a case opened on one would
        # never see the conflict this test is about.
        matched = session.scalar(
            select(MatchResult)
            .where(
                MatchResult.run_id == first.run_id,
                MatchResult.decision == Decision.MATCH,
                MatchResult.route == DbRoute.PROBABILISTIC,
            )
            .limit(1)
        )
        assert matched is not None, "the slice produced no probabilistic match to open a case on"
        case = open_case(
            session,
            provider_id=matched.chosen_provider_id or "",
            sanction_record_id=matched.sanction_record_id,
            match_result_id=matched.id,
        )
        session.commit()

        second = reconcile(
            session,
            settings,
            RunRequest(
                strategy="probabilistic",
                limit=SLICE,
                config_version=config_b_version,
                show_progress=False,
            ),
        )

        fixture = Fixture(
            settings=settings,
            run_a=first.run_id,
            run_b=second.run_id,
            case_id=case.id,
            result_a_id=matched.id,
            config_b_id=config_b_id,
            sanction_record_id=matched.sanction_record_id,
        )

    yield fixture

    with session_scope(settings) as session:
        session.execute(delete(AuditLog).where(AuditLog.entity_id == str(fixture.case_id)))
        session.execute(delete(Case).where(Case.id == fixture.case_id))
        session.execute(
            delete(ReconciliationRun).where(
                ReconciliationRun.id.in_([fixture.run_a, fixture.run_b])
            )
        )
        session.execute(delete(ScoringConfigRow).where(ScoringConfigRow.id == fixture.config_b_id))


def _strict_copy(base: Any) -> Any:
    """The same fitted model with an unreachable accept threshold.

    A config that cannot accept anything turns every `MATCH` into `AMBIGUOUS`,
    which is a decision change the diff and the conflict flag must both notice.
    It is a deliberately blunt instrument: the test is about what the system
    does when a config changes, not about the plausibility of this one.
    """
    from concordance.db.models import ScoringConfig as ScoringConfigRow

    params = copy.deepcopy(dict(base.params))
    for thresholds in params["thresholds"].values():
        thresholds["t_auto_accept"] = 1.01
    params["config_id"] = f"{base.version}_strict_test"
    return ScoringConfigRow(
        version=params["config_id"],
        params=params,
        t_auto_accept=1.01,
        t_auto_reject=base.t_auto_reject,
        calibrator=dict(base.calibrator or {}),
        fitted_from=base.fitted_from,
        notes="created by tests/integration/test_reconciliation_runs.py",
    )


# --------------------------------------------------------------------------
# the run itself
# --------------------------------------------------------------------------


def test_a_run_records_the_provenance_replay_needs(scenario: Fixture) -> None:
    from concordance.db.models import ReconciliationRun
    from concordance.db.session import session_scope
    from concordance.matching.engine import ENGINE_VERSION

    with session_scope(scenario.settings) as session:
        run = session.get(ReconciliationRun, scenario.run_a)
        assert run is not None
        assert run.status == "COMPLETED"
        assert run.engine_version == ENGINE_VERSION
        assert run.scoring_config_id is not None
        assert run.provider_snapshot_hash and len(run.provider_snapshot_hash) == 64
        assert run.sanction_snapshot_hash and len(run.sanction_snapshot_hash) == 64
        assert run.strategy == "probabilistic"
        assert run.request["limit"] == SLICE
        assert run.records_total == SLICE
        assert (
            run.matched_count + run.ambiguous_count + run.no_match_count == run.records_total
        ), "every record landed in exactly one outcome"


def test_a_run_stores_its_whole_candidate_pair_tally(scenario: Fixture) -> None:
    """Every blocked pair, not only the top-k kept as candidates: retuning fits on it."""
    from sqlalchemy import func, select

    from concordance.db.models import MatchCandidate, MatchResult
    from concordance.db.repositories.configs import ConfigRepository
    from concordance.db.session import session_scope
    from concordance.matching.comparators import LEVEL_COUNTS

    with session_scope(scenario.settings) as session:
        tally = ConfigRepository(session).patterns(scenario.run_a)
        stored = session.scalar(
            select(func.count())
            .select_from(MatchCandidate)
            .join(MatchResult, MatchResult.id == MatchCandidate.match_result_id)
            .where(MatchResult.run_id == scenario.run_a)
        )
    assert tally, "the run wrote no pattern tally"
    pairs = sum(n for rows in tally.values() for _, n in rows)
    assert pairs >= (stored or 0) > 0
    for kind, rows in tally.items():
        sizes = LEVEL_COUNTS[kind]
        for vector, n in rows:
            assert n > 0
            assert len(vector) == len(sizes)
            assert all(0 <= level < size for level, size in zip(vector, sizes, strict=True))


def test_the_audit_sample_draws_only_auto_rejects_that_had_candidates(scenario: Fixture) -> None:
    from sqlalchemy import select

    from concordance.db.models import MatchResult, ReconciliationRun
    from concordance.db.session import session_scope

    with session_scope(scenario.settings) as session:
        run = session.get(ReconciliationRun, scenario.run_a)
        assert run is not None and run.audit_rate == scenario.settings.AUDIT_RATE
        drawn = session.scalars(
            select(MatchResult).where(
                MatchResult.run_id == scenario.run_a, MatchResult.audit_sampled.is_(True)
            )
        ).all()
    assert all(r.decision == "NO_MATCH" and r.raw_match_weight is not None for r in drawn)


def test_a_newer_config_does_not_score_runs_until_it_is_activated(scenario: Fixture) -> None:
    """The strict config the fixture wrote is the newest row, and it is not live."""
    from concordance.db.repositories.configs import ConfigRepository
    from concordance.db.session import session_scope
    from concordance.jobs.reconcile import ensure_scoring_config

    with session_scope(scenario.settings) as session:
        active = ensure_scoring_config(session, scenario.settings)
        assert active.id != scenario.config_b_id
        assert ConfigRepository(session).active() is not None


def test_candidates_are_persisted_with_their_evidence(scenario: Fixture) -> None:
    """The Investigation UI renders these; a result without them explains nothing."""
    from concordance.db.repositories.matches import MatchRepository
    from concordance.db.session import session_scope

    with session_scope(scenario.settings) as session:
        candidates = MatchRepository(session).candidates_for(scenario.result_a_id)
        assert candidates, "the match kept no candidates"
        assert [c.rank for c in candidates] == sorted(c.rank for c in candidates)
        top = candidates[0]
        assert top.rank == 1
        assert top.field_levels, "no agreement levels stored"
        assert top.field_weights, "no per-field weights stored"


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def test_replay_is_decision_identical_and_calls_no_model(scenario: Fixture) -> None:
    from concordance.db.session import session_scope
    from concordance.jobs.replay import replay

    with session_scope(scenario.settings) as session:
        report = replay(session, scenario.settings, scenario.run_a)

    assert report.decision_identical, report.drifts[:5]
    assert report.compared == SLICE
    assert report.identical == SLICE
    assert report.drifted == 0
    assert report.missing == 0
    assert report.llm_calls == 0
    assert report.hashes_match


def test_replay_refuses_when_the_snapshot_no_longer_matches(scenario: Fixture) -> None:
    """The refusal is the feature: a replay against changed data proves nothing."""
    from concordance.db.models import ReconciliationRun
    from concordance.db.session import session_scope
    from concordance.jobs.replay import SnapshotDriftError, replay

    with session_scope(scenario.settings) as session:
        run = session.get(ReconciliationRun, scenario.run_a)
        original = run.provider_snapshot_hash
        run.provider_snapshot_hash = "0" * 64
        session.commit()
        try:
            with pytest.raises(SnapshotDriftError, match="comparing like with like"):
                replay(session, scenario.settings, scenario.run_a)
        finally:
            run.provider_snapshot_hash = original
            session.commit()


# --------------------------------------------------------------------------
# supersede, conflict, diff (Q5)
# --------------------------------------------------------------------------


def test_the_re_run_supersedes_without_deleting_anything(scenario: Fixture) -> None:
    from concordance.db.repositories.matches import MatchRepository
    from concordance.db.session import session_scope

    with session_scope(scenario.settings) as session:
        repo = MatchRepository(session)
        history = repo.history_for_record(scenario.sanction_record_id)
        assert len(history) >= 2, "the earlier decision was overwritten rather than superseded"

        old = repo.get_result(scenario.result_a_id)
        assert old is not None, "the original result was deleted"
        assert old.superseded_by is not None
        assert old.superseded_at is not None
        assert old.decision == "MATCH", "the superseded row still says what it said"

        current = repo.current_for_record(scenario.sanction_record_id)
        assert current is not None
        assert current.run_id == scenario.run_b
        assert current.id == old.superseded_by


def test_the_active_case_survives_the_re_run_untouched_but_flagged(scenario: Fixture) -> None:
    """A config change must never silently retract a decision a human made."""
    from concordance.db.models import Case
    from concordance.db.session import session_scope

    with session_scope(scenario.settings) as session:
        case = session.get(Case, scenario.case_id)
        assert case is not None
        assert case.status == "ACTIVE", "the re-run closed a case it has no business closing"
        assert case.match_result_id == scenario.result_a_id, "the case still cites its evidence"
        assert case.conflict_flag is True
        assert case.conflict_match_result_id is not None
        assert case.conflict_match_result_id != scenario.result_a_id


def test_the_conflict_is_audited_with_a_system_actor(scenario: Fixture) -> None:
    from concordance.db.repositories.audit import AuditRepository
    from concordance.db.session import session_scope

    with session_scope(scenario.settings) as session:
        page = AuditRepository(session).for_entity("case", str(scenario.case_id), limit=20)
        actions = {row.action for row in page.items}
        assert "case.conflict_flagged" in actions
        flagged = next(row for row in page.items if row.action == "case.conflict_flagged")
        assert flagged.actor_user_id is None
        assert flagged.actor_role == "system"
        assert flagged.before["decision"] == "MATCH"
        assert flagged.after["decision"] != flagged.before["decision"]


def test_diff_names_the_config_change_that_moved_the_decisions(scenario: Fixture) -> None:
    from concordance.db.session import session_scope
    from concordance.jobs.diff import diff_runs

    with session_scope(scenario.settings) as session:
        report = diff_runs(session, scenario.run_a, scenario.run_b)

    assert report.changed_decision, "raising the accept threshold changed nothing"
    assert not report.new_records
    assert not report.removed_records

    delta = report.config.as_dict()
    assert "scoring_config" in delta, "the diff did not say which config changed"
    assert delta["t_auto_accept"]["after"] == 1.01
    assert "request.config_version" in delta

    change = report.changed_decision[0]
    assert change.before.decision == "MATCH"
    assert change.after.decision in {"AMBIGUOUS", "NO_MATCH"}
    assert report.as_dict()["counts"]["changed_decision"] == len(report.changed_decision)


# --------------------------------------------------------------------------
# case expiry (Q3)
# --------------------------------------------------------------------------


def test_expiry_transitions_a_past_dated_case_and_audits_it(scenario: Fixture) -> None:
    from concordance.cases.lifecycle import expire_cases
    from concordance.db.models import Case
    from concordance.db.repositories.audit import AuditRepository
    from concordance.db.session import session_scope

    today = datetime.now(UTC).date()
    with session_scope(scenario.settings) as session:
        case = session.get(Case, scenario.case_id)
        case.end_date = today - timedelta(days=1)
        session.commit()

        report = expire_cases(session, today=today)
        session.commit()

        assert report.expired >= 1
        assert str(scenario.case_id) in report.case_ids
        assert session.get(Case, scenario.case_id).status == "EXPIRED"

        page = AuditRepository(session).for_entity("case", str(scenario.case_id), limit=20)
        expired = next(row for row in page.items if row.action == "case.expired")
        assert expired.actor_user_id is None
        assert expired.actor_role == "system"
        assert expired.before["status"] == "ACTIVE"
        assert expired.after["status"] == "EXPIRED"


def test_a_second_expiry_run_the_same_day_transitions_nothing(scenario: Fixture) -> None:
    """Idempotent, because the scheduler may well enqueue it twice."""
    from concordance.cases.lifecycle import expire_cases
    from concordance.db.session import session_scope

    with session_scope(scenario.settings) as session:
        report = expire_cases(session, today=datetime.now(UTC).date())
        session.commit()
    assert report.expired == 0
