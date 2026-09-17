"""Fit, calibrate, score and report, on a dataset small enough to run in CI.

The unit tests prove each piece behaves; this proves they compose. It runs on a
reduced dataset - 4,000 providers rather than 50,000 - so it finishes in
seconds, and the properties it asserts are the ones that must hold at any scale:
the fit is reproducible, calibration improves the numbers, the probabilistic
engine beats the baselines, and the config JSON survives a round trip to disk
and back into a working engine.
"""

from __future__ import annotations

import json

import pytest

from concordance.domain import Outcome
from concordance.eval.fitting import collect_patterns, fit_config
from concordance.eval.harness import evaluate, write_report
from concordance.eval.pairs import prepare
from concordance.eval.report_html import load_sweep, render, write_html
from concordance.matching.comparators import ModelKind
from concordance.matching.scoring_config import ScoringConfig
from concordance.matching.strategies import ALL_STRATEGIES, StrategyName, build_strategy
from concordance.synth.pipeline import seed_dataset

SEED = 20260914
PROVIDERS = 4_000
SANCTIONS = 900

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> object:
    out = tmp_path_factory.mktemp("stage3")
    seed_dataset(
        providers=PROVIDERS,
        sanctions=SANCTIONS,
        corruption=0.5,
        seed=SEED,
        out_dir=out,
        write_excel=False,
    )
    return out


@pytest.fixture(scope="module")
def prepared(dataset):
    return prepare(dataset, max_candidates=30, show_progress=False)


@pytest.fixture(scope="module")
def fitted(prepared):
    return fit_config(prepared, seed=SEED)


# --------------------------------------------------------------------------
# the fit
# --------------------------------------------------------------------------


def test_both_models_are_fitted_independently(prepared, fitted) -> None:
    """PLAN 11.2: two fits, neither contaminating the other."""
    assert set(fitted.models) == {ModelKind.INDIVIDUAL, ModelKind.ORGANIZATION}
    individual = fitted.models[ModelKind.INDIVIDUAL]
    organization = fitted.models[ModelKind.ORGANIZATION]
    assert individual.fields != organization.fields
    assert individual.n_pairs > 0 and organization.n_pairs > 0
    # No organization pair ever taught the individual model that a first name
    # is usually missing - the failure PLAN 11.2 exists to prevent.
    patterns = collect_patterns(prepared.work)
    assert patterns[ModelKind.INDIVIDUAL].total == individual.n_pairs
    assert patterns[ModelKind.ORGANIZATION].total == organization.n_pairs


def test_the_fit_is_reproducible_under_a_seed(prepared, fitted) -> None:
    """GATE 3: identical parameters on a repeated run with the same seed."""
    again = fit_config(prepared, seed=SEED)
    for kind in fitted.models:
        assert again.models[kind].to_dict() == fitted.models[kind].to_dict()
    assert again.config.as_dict()["thresholds"] == fitted.config.as_dict()["thresholds"]


def test_calibration_improves_the_individual_model(fitted) -> None:
    calibration = fitted.calibration[ModelKind.INDIVIDUAL]
    assert calibration.before.ece > calibration.after.ece
    assert calibration.after.ece < 0.05
    assert calibration.n_holdout > 0
    assert calibration.n_fit > 0


def test_thresholds_are_ordered_and_recorded(fitted) -> None:
    for kind, bundle in fitted.config.bundles.items():
        thresholds = bundle.thresholds
        assert thresholds.t_auto_reject <= thresholds.t_auto_accept, kind
        assert 0.0 <= thresholds.grey_band_fraction <= 1.0
        assert thresholds.n_holdout > 0


def test_learned_weights_are_not_uniform(fitted) -> None:
    """If every level scored the same the fit learned nothing."""
    weights = fitted.models[ModelKind.INDIVIDUAL].weights()
    spans = [max(row) - min(row) for row in weights.values()]
    assert all(span > 0.5 for span in spans)
    # A valid NPI agreement must be among the strongest evidence there is -
    # not necessarily the single strongest, since a rare ZIP or a full street
    # address can carry as much, but never middling.
    strongest = sorted((max(row) for row in weights.values()), reverse=True)
    assert weights["npi"][-1] >= strongest[2]
    # And a disagreeing valid NPI must count firmly against the pair.
    assert weights["npi"][2] < 0


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------


def test_config_round_trips_through_disk_into_a_working_engine(fitted, prepared, tmp_path) -> None:
    path = fitted.config.write(tmp_path / "config.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    # The `scoring_configs` row shape, keyed per model - no reshaping at Stage 5.
    assert set(payload["params"]) == {"individual", "organization"}
    assert set(payload["calibrator"]) == {"individual", "organization"}
    assert set(payload["thresholds"]) == {"individual", "organization"}
    assert payload["fitted_from"] == "em"
    assert payload["dataset"]["provider_snapshot_hash"]

    restored = ScoringConfig.read(path)
    assert restored.as_dict() == fitted.config.as_dict()

    work = prepared.work[0]
    a = fitted.config.engine().score_record(work.normalized, work.candidates)
    b = restored.engine().score_record(work.normalized, work.candidates)
    assert a.as_dict() == b.as_dict()


def test_convergence_log_travels_with_the_config(fitted) -> None:
    payload = fitted.config.as_dict()
    trace = payload["params"]["individual"]["convergence"]
    assert trace and {"iteration", "log_likelihood", "lambda"} <= set(trace[0])


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reports(prepared, fitted) -> dict:
    engine = fitted.config.engine()
    return {
        name: evaluate(
            prepared,
            build_strategy(name, engine),
            config_id=fitted.config.config_id,
            seed=SEED,
            show_progress=False,
        )
        for name in ALL_STRATEGIES
    }


def test_probabilistic_beats_both_baselines_by_a_clear_margin(reports) -> None:
    """GATE 3, at corruption 0.5."""
    probabilistic = reports[StrategyName.PROBABILISTIC].overall
    fuzzy = reports[StrategyName.FUZZY].overall
    deterministic = reports[StrategyName.DETERMINISTIC].overall
    assert probabilistic.f1 > fuzzy.f1 + 0.2
    assert probabilistic.f1 > deterministic.f1 + 0.2
    assert probabilistic.recall > fuzzy.recall


def test_the_llm_cell_runs_and_reports_no_calls(reports) -> None:
    """The null adjudicator makes the fourth strategy measurable before Stage 4."""
    assisted = reports[StrategyName.PROBABILISTIC_LLM]
    assert assisted.adjudicator["adjudicator"] == "null"
    assert assisted.adjudicator["adjudicated"] == 0
    assert assisted.adjudicator["cost_usd"] == 0.0
    assert assisted.overall.f1 == pytest.approx(reports[StrategyName.PROBABILISTIC].overall.f1)


def test_metrics_are_reported_per_model_and_per_scenario(reports) -> None:
    report = reports[StrategyName.PROBABILISTIC]
    assert set(report.by_model) >= {"individual", "organization"}
    # Every scenario the generator produced must appear in the breakdown.
    assert len(report.by_scenario) >= 8
    assert all(tally.n > 0 for tally in report.by_scenario.values())
    # The slices partition the same records: neither double-counts nor drops.
    assert sum(t.n for t in report.by_model.values()) == report.overall.n
    assert sum(t.n for t in report.by_scenario.values()) == report.overall.n


def test_exact_npi_scenario_is_essentially_perfect(reports) -> None:
    """If the identifier path is not trustworthy nothing downstream is."""
    tally = reports[StrategyName.PROBABILISTIC].by_scenario["exact_npi"]
    assert tally.precision == pytest.approx(1.0, abs=0.02)
    assert tally.recall > 0.9


def test_confusion_matrix_accounts_for_every_record(reports, prepared) -> None:
    report = reports[StrategyName.PROBABILISTIC]
    total = sum(sum(row.values()) for row in report.confusion.values())
    assert total == len(prepared)


def test_blocking_recall_is_carried_through(reports) -> None:
    report = reports[StrategyName.PROBABILISTIC]
    assert 0.0 < report.blocking_recall <= 1.0
    assert report.blocking_evaluated > 0
    # Recall cannot exceed the ceiling blocking set for it.
    assert report.overall.recall <= report.blocking_recall + 1e-9


def test_ambiguous_is_scored_as_its_own_outcome(reports) -> None:
    report = reports[StrategyName.PROBABILISTIC]
    assert report.overall.ambiguous_expected > 0
    assert 0.0 <= report.overall.ambiguous_accuracy <= 1.0
    ambiguous_row = report.confusion[str(Outcome.AMBIGUOUS)]
    assert sum(ambiguous_row.values()) == report.overall.ambiguous_expected


def test_latency_is_measured_per_stage(reports) -> None:
    latency = reports[StrategyName.PROBABILISTIC].latency_ms
    assert {
        "1_index_build",
        "2_normalize",
        "3_block",
        "4_compare_and_score",
        "total_per_record",
    } <= set(latency)
    assert all(value >= 0 for value in latency.values())


# --------------------------------------------------------------------------
# the artifacts
# --------------------------------------------------------------------------


def test_json_payload_is_the_eval_runs_row_shape(reports, tmp_path) -> None:
    path = write_report(reports[StrategyName.PROBABILISTIC], tmp_path / "eval.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "corruption_level",
        "strategy",
        "precision",
        "recall",
        "f1",
        "false_positives",
        "false_negatives",
        "brier",
        "ece",
        "reliability_bins",
    }
    assert required <= set(payload)
    assert isinstance(payload["reliability_bins"], list)
    assert payload["reliability_bins"]


def test_html_report_is_self_contained(reports, fitted, tmp_path) -> None:
    path = write_html(
        reports[StrategyName.PROBABILISTIC], tmp_path / "eval.html", fitted.config.as_dict()
    )
    page = path.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    # No network: every chart is inline SVG and every style is in the document.
    assert "<svg" in page
    for external in ("http://", "https://", "<script"):
        assert external not in page
    assert "reliability" in page.lower()
    assert "by scenario" in page.lower()


def test_html_report_survives_an_absent_sweep(reports, tmp_path) -> None:
    """The robustness panel degrades to a note rather than an empty chart."""
    assert load_sweep(tmp_path / "missing.json") == []
    page = render(reports[StrategyName.PROBABILISTIC], None, [])
    assert "No sweep results found" in page


def test_html_report_draws_the_robustness_curve_when_a_sweep_exists(reports, tmp_path) -> None:
    sweep_rows = [
        {"corruption_level": level / 10, "strategy": name, "f1": 0.9 - level / 40}
        for level in range(10)
        for name in ("fuzzy", "probabilistic")
    ]
    page = render(reports[StrategyName.PROBABILISTIC], None, sweep_rows)
    assert "No sweep results found" not in page
    assert "corruption level" in page
