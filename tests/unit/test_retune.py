"""Semi-supervised EM, weighted calibration, and the retune's guards."""

from __future__ import annotations

import random

import pytest

from concordance.learning.retune import (
    Label,
    NotEnoughLabelsError,
    _without,
    check_enough,
    recommend,
    vector_from_levels,
)
from concordance.matching.calibration import (
    choose_thresholds,
    denoise,
    expected_thresholds,
    fit_isotonic,
    reliability,
)
from concordance.matching.comparators import FIELD_NAMES, LEVEL_COUNTS, ModelKind, describe
from concordance.matching.fellegi_sunter import (
    ClampedPatterns,
    PatternCounts,
    fit_em,
)

KIND = ModelKind.INDIVIDUAL
SIZES = LEVEL_COUNTS[KIND]


def _vector(rng: random.Random, agree: bool) -> tuple[int, ...]:
    """Top levels for a match, bottom levels for a non-match, with some noise."""
    out = []
    for size in SIZES:
        top = size - 1
        if agree:
            out.append(top if rng.random() < 0.85 else rng.randrange(size))
        else:
            out.append(0 if rng.random() < 0.85 else rng.randrange(size))
    return tuple(out)


def _pairs(n_match: int, n_non: int, seed: int = 1) -> list[tuple[tuple[int, ...], int]]:
    rng = random.Random(seed)
    return [(_vector(rng, True), 1) for _ in range(n_match)] + [
        (_vector(rng, False), 0) for _ in range(n_non)
    ]


# --------------------------------------------------------------------------
# semi-supervised EM
# --------------------------------------------------------------------------


def test_with_every_pair_labelled_lambda_is_the_labelled_share() -> None:
    pairs = _pairs(60, 540)
    clamped = ClampedPatterns.from_labels(KIND, pairs)
    empty = PatternCounts(KIND, (), ())
    model = fit_em(empty, seed=3, restarts=2, clamped=clamped)
    assert model.lam == pytest.approx(60 / 600)
    assert model.n_pairs == 600


def test_labels_name_the_match_component_even_against_the_agreement_heuristic() -> None:
    """Unsupervised EM picks the high-agreement component as matches; labels overrule it."""
    rng = random.Random(5)
    # Deliberately perverse: what the labels call matches *disagree*.
    pairs = [(_vector(rng, False), 1) for _ in range(80)] + [
        (_vector(rng, True), 0) for _ in range(320)
    ]
    clamped = ClampedPatterns.from_labels(KIND, pairs)
    model = fit_em(PatternCounts(KIND, (), ()), seed=3, restarts=1, clamped=clamped)
    name = FIELD_NAMES[KIND][1]
    # m puts its mass on the bottom level, because that is what the labelled
    # matches look like; the label-switch step must not have swapped it back.
    assert model.m[name][0] > model.m[name][-1]
    assert model.lam == pytest.approx(0.2)


def test_the_fit_is_deterministic_and_starts_from_the_parent() -> None:
    pairs = _pairs(100, 900, seed=2)
    data = PatternCounts.from_vectors(KIND, [v for v, _ in pairs])
    parent = fit_em(data, seed=11, restarts=2)
    labelled = ClampedPatterns.from_labels(KIND, pairs[:40] + pairs[-80:])
    a = fit_em(data, seed=11, restarts=2, clamped=labelled, init=parent)
    b = fit_em(data, seed=11, restarts=2, clamped=labelled, init=parent)
    assert a.to_dict() == b.to_dict()
    assert a.warm_started
    assert a.n_pairs == data.total + labelled.total


def test_clamped_patterns_aggregate_duplicates() -> None:
    clamped = ClampedPatterns.from_labels(KIND, [((1,) * 8, 1), ((1,) * 8, 1), ((1,) * 8, 0)])
    assert clamped.total == 3
    assert clamped.positives == 2
    assert len(clamped.patterns) == 2


# --------------------------------------------------------------------------
# weighted calibration: a weight of two is the same point twice
# --------------------------------------------------------------------------


def test_weighted_isotonic_equals_duplicated_points() -> None:
    rng = random.Random(9)
    xs = [rng.uniform(-10, 10) for _ in range(200)]
    ys = [1 if rng.random() < 1 / (1 + 2.718 ** -x) else 0 for x in xs]
    ws = [2.0 if i % 3 == 0 else 1.0 for i in range(len(xs))]
    dup_x = [x for x, w in zip(xs, ws, strict=True) for _ in range(int(w))]
    dup_y = [y for y, w in zip(ys, ws, strict=True) for _ in range(int(w))]
    weighted = fit_isotonic(xs, ys, 0.5, ws)
    duplicated = fit_isotonic(dup_x, dup_y, 0.5)
    for probe in (-8.0, -2.5, 0.0, 1.25, 6.0):
        assert weighted.apply(probe) == pytest.approx(duplicated.apply(probe))


def test_weighted_reliability_and_thresholds_equal_duplicated_points() -> None:
    rng = random.Random(4)
    conf = [rng.random() for _ in range(300)]
    ys = [1 if rng.random() < c else 0 for c in conf]
    ws = [3.0 if c < 0.2 else 1.0 for c in conf]
    dup_c = [c for c, w in zip(conf, ws, strict=True) for _ in range(int(w))]
    dup_y = [y for y, w in zip(ys, ws, strict=True) for _ in range(int(w))]

    a = reliability(conf, ys, 10, ws)
    b = reliability(dup_c, dup_y, 10)
    assert a.ece == pytest.approx(b.ece)
    assert a.brier == pytest.approx(b.brier)

    t1 = choose_thresholds(conf, ys, 0.9, 0.95, ws)
    t2 = choose_thresholds(dup_c, dup_y, 0.9, 0.95)
    assert t1.t_auto_accept == pytest.approx(t2.t_auto_accept)
    assert t1.t_auto_reject == pytest.approx(t2.t_auto_reject)
    assert t1.grey_band_fraction == pytest.approx(t2.grey_band_fraction)


def test_no_weights_changes_nothing() -> None:
    rng = random.Random(8)
    conf = [rng.random() for _ in range(100)]
    ys = [1 if rng.random() < c else 0 for c in conf]
    assert choose_thresholds(conf, ys) == choose_thresholds(conf, ys, weights=[1.0] * 100)
    assert reliability(conf, ys) == reliability(conf, ys, weights=[1.0] * 100)


# --------------------------------------------------------------------------
# reviewer error
# --------------------------------------------------------------------------


def test_denoised_labels_sum_to_the_true_count_in_expectation() -> None:
    rng = random.Random(12)
    eps = 0.05
    truth = [1] * 4_000 + [0] * 16_000
    observed = [1 - y if rng.random() < eps else y for y in truth]
    estimate = sum(denoise(observed, eps))
    assert estimate == pytest.approx(sum(truth), rel=0.03)
    assert denoise([0, 1], 0.0) == [0.0, 1.0]
    with pytest.raises(ValueError):
        denoise([1], 0.5)


def test_noisy_labels_push_the_accept_threshold_to_the_top_unless_denoised() -> None:
    """The failure the first simulation hit, reduced to its arithmetic.

    A perfectly separating score, labels 3% wrong: read as-is, precision over
    the matches is about 97%, so 99% is only "met" in some thin top slice that
    happens to hold no flipped label. The accept threshold climbs there and
    half the volume lands in the grey band. De-noised, the threshold sits at
    the real boundary.
    """
    rng = random.Random(21)
    eps = 0.03
    conf = [0.95 + 0.05 * rng.random() for _ in range(600)] + [
        0.05 * rng.random() for _ in range(600)
    ]
    truth = [1] * 600 + [0] * 600
    noisy = [1 - y if rng.random() < eps else y for y in truth]

    raw = choose_thresholds(conf, noisy, target_precision=0.99, target_recall=0.95)
    fixed = choose_thresholds(conf, denoise(noisy, eps), target_precision=0.99, target_recall=0.95)
    assert raw.t_auto_accept > 0.99
    assert fixed.precision_target_met
    assert fixed.t_auto_accept < 0.96
    assert fixed.grey_band_fraction < raw.grey_band_fraction / 4


def test_expected_thresholds_match_labelled_thresholds_when_labels_follow_the_confidence() -> None:
    """Calibrated scores are their own expected labels: the two searches must agree."""
    rng = random.Random(40)
    conf = [rng.betavariate(0.4, 0.4) for _ in range(20_000)]
    drawn = [1 if rng.random() < c else 0 for c in conf]
    expected = expected_thresholds(conf, 0.95, 0.95)
    labelled = choose_thresholds(conf, drawn, 0.95, 0.95)
    assert expected.precision_target_met and expected.recall_target_met
    assert expected.t_auto_accept == pytest.approx(labelled.t_auto_accept, abs=0.03)
    assert expected.t_auto_reject == pytest.approx(labelled.t_auto_reject, abs=0.03)
    above = [c for c in conf if c >= expected.t_auto_accept]
    assert sum(above) / len(above) >= 0.95


def test_expected_thresholds_say_when_the_target_is_out_of_reach() -> None:
    placed = expected_thresholds([0.5] * 100, 0.99, 0.99)
    assert not placed.precision_target_met
    assert placed.achieved_precision == pytest.approx(0.5)
    assert expected_thresholds([], 0.99).n_holdout == 0


def test_noisy_ssem_recovers_lambda_where_a_hard_clamp_is_pulled_off() -> None:
    rng = random.Random(33)
    eps = 0.05
    clean = _pairs(150, 850, seed=7)
    noisy = [(v, 1 - y if rng.random() < eps else y) for v, y in clean]
    clamped = ClampedPatterns.from_labels(KIND, noisy)
    empty = PatternCounts(KIND, (), ())
    aware = fit_em(empty, seed=2, restarts=1, clamped=clamped, label_noise=eps)
    hard = fit_em(empty, seed=2, restarts=1, clamped=clamped)
    assert abs(aware.lam - 0.15) < abs(hard.lam - 0.15)
    assert aware.lam == pytest.approx(0.15, abs=0.02)


# --------------------------------------------------------------------------
# the activation gate
# --------------------------------------------------------------------------


def _holdout(precision: float, recall: float, missed: float, review: float, n: int = 200) -> dict[str, float | int]:
    return {"n": n, "precision": precision, "recall": recall, "missed": missed, "review_share": review}


def test_a_config_that_buys_precision_with_review_load_is_not_recommended() -> None:
    """The noisy-label failure: thin evidence, so it reviews most of the volume."""
    out = recommend(
        _holdout(1.0, 0.63, 0.0, 0.44), _holdout(0.956, 0.84, 0.011, 0.21), target_precision=0.99
    )
    assert out["activate"] is False
    assert "review" in out["reason"]


def test_losing_precision_is_refused() -> None:
    out = recommend(_holdout(0.93, 0.95, 0.01, 0.10), _holdout(0.99, 0.90, 0.01, 0.12), 0.99)
    assert out["activate"] is False
    assert "precision" in out["reason"]


def test_missing_more_true_matches_is_refused_beyond_the_tolerance() -> None:
    out = recommend(_holdout(0.99, 0.95, 0.05, 0.10), _holdout(0.99, 0.90, 0.01, 0.12), 0.99)
    assert out["activate"] is False
    assert "miss more" in out["reason"]
    # Inside the tolerance, the rest of the trade can pay for it.
    ok = recommend(_holdout(0.99, 0.95, 0.025, 0.10), _holdout(0.99, 0.90, 0.01, 0.12), 0.99)
    assert ok["activate"] is True


def test_a_real_gain_is_recommended_and_says_what_it_gained() -> None:
    out = recommend(_holdout(0.99, 0.95, 0.01, 0.08), _holdout(0.985, 0.90, 0.01, 0.14), 0.99)
    assert out["activate"] is True
    assert "recall" in out["reason"] and "review" in out["reason"]


def test_no_measurable_difference_keeps_the_parent() -> None:
    same = _holdout(0.99, 0.90, 0.01, 0.12)
    assert recommend(dict(same), dict(same), 0.99)["activate"] is False
    assert recommend(_holdout(0.0, 0.0, 0.0, 0.0, n=0), same, 0.99)["activate"] is False


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------


def test_a_stored_level_description_turns_back_into_the_vector() -> None:
    vector = (2, 1, 0, 3, 1, 1, 0, 2)
    vector = tuple(min(v, size - 1) for v, size in zip(vector, SIZES, strict=True))
    assert vector_from_levels(KIND, describe(vector, KIND)) == vector
    broken = dict(describe(vector, KIND))
    broken[FIELD_NAMES[KIND][0]] = "NOT_A_LEVEL"
    assert vector_from_levels(KIND, broken) is None
    missing = dict(describe(vector, KIND))
    missing.pop(FIELD_NAMES[KIND][2])
    assert vector_from_levels(KIND, missing) is None


def _label(i: int, y: int) -> Label:
    return Label(key=f"r{i}", kind=KIND, vector=(0,) * len(SIZES), label=y)


def test_too_few_labels_is_refused_with_the_counts() -> None:
    with pytest.raises(NotEnoughLabelsError) as refused:
        check_enough([_label(i, i % 2) for i in range(40)], min_labels=100)
    assert refused.value.counts["total"] == 40


def test_one_class_only_is_refused() -> None:
    with pytest.raises(NotEnoughLabelsError, match="true and"):
        check_enough([_label(i, 1) for i in range(150)], min_labels=100)
    check_enough([_label(i, int(i < 20)) for i in range(150)], min_labels=100)


def test_labelled_pairs_are_taken_out_of_the_tally_once_each() -> None:
    a, b = (0,) * len(SIZES), (1,) * len(SIZES)
    data = PatternCounts(KIND, (a, b), (5, 1))
    labelled = [
        Label("x", KIND, a, 0, in_tally=True),
        Label("y", KIND, b, 1, in_tally=True),
        Label("z", KIND, b, 1, in_tally=True),
    ]
    out = _without(data, labelled)
    assert dict(zip(out.patterns, out.counts, strict=True)) == {a: 4}
