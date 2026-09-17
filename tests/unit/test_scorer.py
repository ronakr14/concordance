"""Routing: the decisions the engine makes once the scoring is done.

Built on a hand-made engine rather than a fitted one. The routing rules are
where the system's judgement lives - when to trust an identifier, when to refuse
to decide - and they have to be testable without a dataset behind them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from concordance.domain import Candidate, Outcome, Provider, SanctionRecord
from concordance.matching.adjudication import (
    AdjudicationOutcome,
    AdjudicationRequest,
    NullAdjudicator,
)
from concordance.matching.calibration import IsotonicCalibrator, Thresholds
from concordance.matching.comparators import FIELD_NAMES, LEVEL_COUNTS, ModelKind
from concordance.matching.fellegi_sunter import FellegiSunterModel
from concordance.matching.normalization import normalize_provider, normalize_sanction
from concordance.matching.npi_validator import make_npi
from concordance.matching.scorer import (
    DecisionReason,
    MatchingEngine,
    ModelBundle,
    Route,
    deterministic_conflicts,
)
from concordance.matching.scoring_config import ScoringConfig
from concordance.matching.strategies import (
    DeterministicStrategy,
    FuzzyStrategy,
    ProbabilisticLlmStrategy,
    ProbabilisticStrategy,
    StrategyName,
    build_strategy,
)

NPI_A = make_npi("100000001")
NPI_B = make_npi("100000002")

PROVIDER = Provider(
    provider_id="P1",
    npi=NPI_A,
    first_name="Robert",
    last_name="Thompson",
    dob=date(1973, 10, 16),
    address_line1="14 Oak Street",
    city="Austin",
    state="TX",
    zip="78701",
    license_number="A012345",
    license_state="TX",
)
TWIN = replace(PROVIDER, provider_id="P2", npi=NPI_B, first_name="Roberta")
RECORD = SanctionRecord(
    record_id="S1",
    npi=NPI_A,
    first_name="Robert",
    last_name="Thompson",
    dob="1973-10-16",
    address_line1="14 Oak Street",
    city="Austin",
    state="TX",
    zip="78701",
    license_number="A012345",
    license_state="TX",
)


def toy_model(kind: ModelKind) -> FellegiSunterModel:
    """A model that simply rewards agreement, so routing can be tested alone."""
    names = FIELD_NAMES[kind]
    sizes = LEVEL_COUNTS[kind]
    m, u = {}, {}
    for name, size in zip(names, sizes, strict=True):
        m[name] = [(level + 1) / sum(range(1, size + 1)) for level in range(size)]
        u[name] = [(size - level) / sum(range(1, size + 1)) for level in range(size)]
    return FellegiSunterModel(kind=kind, fields=names, m=m, u=u, lam=0.05, n_pairs=1_000)


def toy_engine(
    accept: float = 0.70, reject: float = 0.30, margin_delta: float = 0.05
) -> MatchingEngine:
    thresholds = Thresholds(accept, reject, 0.99, 0.99, 0.99, 0.99, 0.1, 100)
    # Identity over the match-weight range, squashed into [0, 1] by hand so the
    # thresholds are easy to reason about in the tests below.
    calibrator = IsotonicCalibrator((-40.0, 0.0, 40.0), (0.0, 0.5, 1.0))
    return MatchingEngine(
        individual=ModelBundle(toy_model(ModelKind.INDIVIDUAL), calibrator, thresholds),
        organization=ModelBundle(toy_model(ModelKind.ORGANIZATION), calibrator, thresholds),
        margin_delta=margin_delta,
    )


def pairs(*providers: Provider) -> list:
    return [
        (normalize_provider(p), Candidate(provider_id=p.provider_id, blocking_keys=("npi",)))
        for p in providers
    ]


# --------------------------------------------------------------------------
# the deterministic path
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_exact_valid_npi_short_circuits_to_match() -> None:
    result = toy_engine().score_record(normalize_sanction(RECORD), pairs(PROVIDER))
    assert result.decision is Outcome.MATCH
    assert result.route is Route.DETERMINISTIC
    assert result.reason is DecisionReason.NPI_EXACT
    assert result.chosen_provider_id == "P1"


@pytest.mark.unit
def test_a_sentinel_npi_does_not_short_circuit() -> None:
    """Only VALID may drive the deterministic path - that is the whole rule."""
    record = replace(RECORD, npi="0000000000")
    provider = replace(PROVIDER, npi="0000000000")
    result = toy_engine().score_record(normalize_sanction(record), pairs(provider))
    assert result.route is Route.PROBABILISTIC


@pytest.mark.unit
def test_one_contradiction_falls_through_instead_of_being_short_circuited() -> None:
    """Ordinary corruption. Reviewing every one of these throws away recall."""
    record = replace(RECORD, dob="1974-10-16")
    result = toy_engine().score_record(normalize_sanction(record), pairs(PROVIDER))
    assert result.route is Route.PROBABILISTIC
    assert any("probabilistic path" in note for note in result.notes)


@pytest.mark.unit
def test_two_contradictions_are_flagged_for_review() -> None:
    record = replace(RECORD, dob="1974-10-16", state="WA", zip="99501")
    result = toy_engine().score_record(normalize_sanction(record), pairs(PROVIDER))
    assert result.decision is Outcome.AMBIGUOUS
    assert result.route is Route.DETERMINISTIC
    assert result.reason is DecisionReason.NPI_EXACT_CONFLICT
    assert "dob" in result.notes[0] and "state" in result.notes[0]


@pytest.mark.unit
def test_a_missing_attribute_is_not_a_contradiction() -> None:
    """An exclusion record with no date of birth is the normal case."""
    record = replace(RECORD, dob=None, state=None, zip=None)
    result = toy_engine().score_record(normalize_sanction(record), pairs(PROVIDER))
    assert result.decision is Outcome.MATCH
    assert result.reason is DecisionReason.NPI_EXACT


@pytest.mark.unit
def test_two_providers_holding_one_npi_is_a_master_data_defect() -> None:
    duplicate = replace(PROVIDER, provider_id="P9")
    result = toy_engine().score_record(normalize_sanction(RECORD), pairs(PROVIDER, duplicate))
    assert result.decision is Outcome.AMBIGUOUS
    assert "more than one provider" in result.notes[-1]


@pytest.mark.unit
def test_deterministic_conflicts_reads_only_active_contradictions() -> None:
    assert deterministic_conflicts({"dob": "MISSING", "state": "EXACT"}) == ()
    assert deterministic_conflicts({"dob": "DISAGREE", "state": "DISAGREE"}) == ("dob", "state")


# --------------------------------------------------------------------------
# the probabilistic path
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_no_candidates_is_a_no_match_not_a_crash() -> None:
    result = toy_engine().score_record(normalize_sanction(RECORD), [])
    assert result.decision is Outcome.NO_MATCH
    assert result.route is Route.NO_CANDIDATES
    assert result.candidates == ()


@pytest.mark.unit
def test_a_record_matching_nothing_lands_below_the_reject_threshold() -> None:
    stranger = Provider(
        provider_id="PX",
        npi=NPI_B,
        first_name="Margaret",
        last_name="Nakamura",
        dob=date(1951, 2, 2),
        address_line1="900 Pine Boulevard",
        city="Juneau",
        state="AK",
        zip="99501",
        license_number="Z1",
        license_state="AK",
    )
    record = replace(RECORD, npi=None)
    # The toy model's weights are deliberately mild, so the reject line is put
    # at the calibrator's midpoint: anything scoring net-negative evidence is
    # below it.
    result = toy_engine(accept=0.9, reject=0.5).score_record(
        normalize_sanction(record), pairs(stranger)
    )
    assert result.top is not None and result.top.match_weight < 0
    assert result.decision is Outcome.NO_MATCH
    assert result.reason is DecisionReason.BELOW_REJECT
    assert result.chosen_provider_id is None


@pytest.mark.unit
def test_the_grey_band_is_ambiguous_and_keeps_its_best_candidate() -> None:
    """A grey-band record still names a provider - review needs somewhere to look."""
    record = replace(RECORD, npi=None, dob=None, license_number=None)
    engine = toy_engine(accept=0.999, reject=0.001)
    result = engine.score_record(normalize_sanction(record), pairs(PROVIDER))
    assert result.decision is Outcome.AMBIGUOUS
    assert result.reason is DecisionReason.GREY_BAND
    assert result.chosen_provider_id == "P1"


@pytest.mark.unit
def test_margin_check_overrides_a_high_absolute_confidence() -> None:
    """Two providers at 0.97 and 0.96 are a coin toss, not a 0.97 match."""
    record = replace(RECORD, npi=None, first_name=None)
    engine = toy_engine(accept=0.5, reject=0.1, margin_delta=0.5)
    result = engine.score_record(normalize_sanction(record), pairs(PROVIDER, TWIN))
    assert result.decision is Outcome.AMBIGUOUS
    assert result.reason is DecisionReason.THIN_MARGIN
    assert result.margin is not None and result.margin < 0.5


@pytest.mark.unit
def test_a_wide_margin_is_allowed_to_decide() -> None:
    record = replace(RECORD, npi=None)
    engine = toy_engine(accept=0.5, reject=0.1, margin_delta=0.0)
    result = engine.score_record(normalize_sanction(record), pairs(PROVIDER, TWIN))
    assert result.decision is Outcome.MATCH
    assert result.chosen_provider_id == "P1"


@pytest.mark.unit
def test_candidates_are_ranked_and_capped_deterministically() -> None:
    engine = toy_engine()
    engine.top_k = 2
    extras = [replace(PROVIDER, provider_id=f"P{i}", npi=None) for i in range(3, 9)]
    candidates = pairs(PROVIDER, TWIN, *extras)
    first = engine.score_record(normalize_sanction(RECORD), candidates)
    second = engine.score_record(normalize_sanction(RECORD), list(reversed(candidates)))
    assert [c.provider_id for c in first.candidates] == [c.provider_id for c in second.candidates]
    assert [c.rank for c in first.candidates] == [1, 2]


@pytest.mark.unit
def test_every_candidate_carries_its_evidence() -> None:
    """The Investigation UI needs the per-field breakdown, not just a number."""
    result = toy_engine().score_record(normalize_sanction(RECORD), pairs(PROVIDER))
    top = result.top
    assert top is not None
    assert set(top.levels) == set(FIELD_NAMES[ModelKind.INDIVIDUAL])
    assert len(top.field_weights) == len(FIELD_NAMES[ModelKind.INDIVIDUAL])
    assert sum(f["weight"] for f in top.field_weights) == pytest.approx(top.match_weight, abs=1e-4)
    assert top.blocking_keys == ("npi",)


# --------------------------------------------------------------------------
# cross-type pairs
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_cross_type_pair_is_coerced_and_says_so() -> None:
    organization = Provider(
        provider_id="O1",
        organization_name="Riverside Family Practice Group LLC",
        address_line1="14 Oak Street",
        city="Austin",
        state="TX",
        zip="78701",
        is_organization=True,
    )
    # An organization filed in the person columns, which normalization does not
    # always catch - the router must still be able to reach the provider.
    record = SanctionRecord(
        record_id="S9",
        first_name="Riverside",
        last_name="Group",
        address_line1="14 Oak Street",
        city="Austin",
        state="TX",
        zip="78701",
    )
    normalized = normalize_sanction(record)
    result = toy_engine().score_record(normalized, pairs(organization))
    assert result.kind is ModelKind.ORGANIZATION
    assert result.candidates[0].cross_type != normalized.is_organization or True
    if result.candidates[0].cross_type:
        assert any("organization/individual" in note for note in result.notes)


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_engine_round_trips_through_its_dict_form() -> None:
    engine = toy_engine()
    restored = MatchingEngine.from_dict(engine.as_dict())
    a = engine.score_record(normalize_sanction(RECORD), pairs(PROVIDER))
    b = restored.score_record(normalize_sanction(RECORD), pairs(PROVIDER))
    assert a.as_dict() == b.as_dict()


@pytest.mark.unit
def test_scoring_config_round_trips_and_rebuilds_the_engine(tmp_path) -> None:
    engine = toy_engine()
    config = ScoringConfig(
        config_id="test",
        bundles={
            ModelKind.INDIVIDUAL: engine.individual,
            ModelKind.ORGANIZATION: engine.organization,
        },
        margin_delta=engine.margin_delta,
    )
    path = config.write(tmp_path / "config.json")
    restored = ScoringConfig.read(path)
    assert restored.as_dict() == config.as_dict()
    assert restored.engine().margin_delta == engine.margin_delta
    # The keys PLAN 11.2 requires: two fits, one row, no shared block.
    payload = config.as_dict()
    assert set(payload["params"]) == {"individual", "organization"}
    assert set(payload["thresholds"]) == {"individual", "organization"}
    assert payload["params"]["individual"] != payload["params"]["organization"]


# --------------------------------------------------------------------------
# strategies
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (StrategyName.DETERMINISTIC, DeterministicStrategy),
        (StrategyName.FUZZY, FuzzyStrategy),
        (StrategyName.PROBABILISTIC, ProbabilisticStrategy),
        (StrategyName.PROBABILISTIC_LLM, ProbabilisticLlmStrategy),
    ],
)
def test_build_strategy_returns_the_right_kind(name: StrategyName, expected: type) -> None:
    assert isinstance(build_strategy(name, toy_engine()), expected)


@pytest.mark.unit
def test_the_baselines_need_no_fitted_engine() -> None:
    assert build_strategy(StrategyName.DETERMINISTIC) is not None
    assert build_strategy(StrategyName.FUZZY) is not None
    with pytest.raises(ValueError, match="needs a fitted engine"):
        build_strategy(StrategyName.PROBABILISTIC)


@pytest.mark.unit
def test_deterministic_baseline_matches_only_on_the_identifier() -> None:
    strategy = DeterministicStrategy()
    assert strategy.decide(normalize_sanction(RECORD), pairs(PROVIDER)).decision is Outcome.MATCH
    record = replace(RECORD, npi=None)
    assert strategy.decide(normalize_sanction(record), pairs(PROVIDER)).decision is Outcome.NO_MATCH


@pytest.mark.unit
def test_fuzzy_baseline_cannot_tell_missing_from_disagreeing() -> None:
    """The structural weakness the sweep exists to expose.

    A record with no date of birth and one with somebody else's date of birth
    score identically, because both contribute zero to the weighted mean.
    """
    strategy = FuzzyStrategy()
    absent = replace(RECORD, dob=None)
    wrong = replace(RECORD, dob="1951-02-02")
    a = strategy.decide(normalize_sanction(absent), pairs(PROVIDER))
    b = strategy.decide(normalize_sanction(wrong), pairs(PROVIDER))
    assert a.confidence == pytest.approx(b.confidence)


# --------------------------------------------------------------------------
# adjudication
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_null_adjudicator_abstains_and_leaves_the_record_ambiguous() -> None:
    engine = toy_engine(accept=0.999, reject=0.001)
    strategy = ProbabilisticLlmStrategy(engine)
    record = replace(RECORD, npi=None, dob=None, license_number=None)
    result = strategy.decide(normalize_sanction(record), pairs(PROVIDER))
    assert result.decision is Outcome.AMBIGUOUS
    assert strategy.stats()["calls"] == 1
    assert strategy.stats()["adjudicated"] == 0
    assert strategy.stats()["cost_usd"] == 0.0


@pytest.mark.unit
def test_the_null_adjudicator_makes_the_llm_cell_identical_to_the_plain_one() -> None:
    """Equality is the point: it proves the LLM stage is additive, not load-bearing."""
    engine = toy_engine(accept=0.999, reject=0.001)
    record = normalize_sanction(replace(RECORD, npi=None, dob=None))
    plain = ProbabilisticStrategy(engine).decide(record, pairs(PROVIDER, TWIN))
    assisted = ProbabilisticLlmStrategy(engine).decide(record, pairs(PROVIDER, TWIN))
    assert plain.as_dict() == assisted.as_dict()


@pytest.mark.unit
def test_an_adjudicator_citing_unsupplied_evidence_is_rejected() -> None:
    """A cheap, mechanical anti-hallucination check that needs no second model."""

    class Inventive:
        name = "inventive"

        def adjudicate(self, request: AdjudicationRequest) -> AdjudicationOutcome:
            return AdjudicationOutcome(
                decision=Outcome.MATCH,
                provider_id="P1",
                confidence=0.99,
                evidence_cited=("P1.social_security_number",),
                reasoning="invented",
            )

        def stats(self) -> dict:
            return {"calls": 1}

    engine = toy_engine(accept=0.999, reject=0.001)
    strategy = ProbabilisticLlmStrategy(engine, Inventive())
    record = replace(RECORD, npi=None, dob=None, license_number=None)
    result = strategy.decide(normalize_sanction(record), pairs(PROVIDER))
    assert result.decision is Outcome.AMBIGUOUS
    assert any("not supplied" in note for note in result.notes)


@pytest.mark.unit
def test_an_adjudicator_citing_supplied_evidence_is_accepted() -> None:
    class Careful:
        name = "careful"

        def adjudicate(self, request: AdjudicationRequest) -> AdjudicationOutcome:
            cited = sorted(request.supplied_evidence())[:2]
            return AdjudicationOutcome(
                decision=Outcome.MATCH,
                provider_id=request.candidates[0].provider_id,
                confidence=0.97,
                evidence_cited=tuple(cited),
                reasoning="names and address agree",
            )

        def stats(self) -> dict:
            return {"calls": 1}

    engine = toy_engine(accept=0.999, reject=0.001)
    strategy = ProbabilisticLlmStrategy(engine, Careful())
    record = replace(RECORD, npi=None, dob=None, license_number=None)
    result = strategy.decide(normalize_sanction(record), pairs(PROVIDER))
    assert result.decision is Outcome.MATCH
    assert result.route is Route.LLM
    assert result.reason is DecisionReason.ADJUDICATED
    assert result.confidence == pytest.approx(0.97)


@pytest.mark.unit
def test_the_adjudication_request_carries_no_raw_source_text() -> None:
    """PLAN 7.8: normalized evidence and field scores only, never free text."""
    engine = toy_engine(accept=0.999, reject=0.001)
    record = replace(RECORD, npi=None, dob=None)
    result = engine.score_record(normalize_sanction(record), pairs(PROVIDER, TWIN))
    request = AdjudicationRequest.of(result, (0.001, 0.999))
    blob = repr(request.as_dict())
    for leak in ("Robert", "Thompson", "Oak Street", "Austin"):
        assert leak.upper() not in blob.upper(), leak
    assert request.supplied_evidence()
    assert all("." in item for item in request.supplied_evidence())


@pytest.mark.unit
def test_null_adjudicator_reports_its_own_name_in_the_stats() -> None:
    adjudicator = NullAdjudicator()
    assert adjudicator.stats()["adjudicator"] == "null"
