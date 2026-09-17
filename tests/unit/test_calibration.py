"""Calibration, isotonic regression and threshold selection.

The tests are built around miscalibrated scores with a known true probability,
so "did calibration help" is a measurement rather than an impression.
"""

from __future__ import annotations

import random
from itertools import pairwise

import pytest

from concordance.matching.calibration import (
    DEFAULT_HOLDOUT,
    IsotonicCalibrator,
    calibrate,
    choose_thresholds,
    fit_isotonic,
    in_holdout,
    log_loss,
    reliability,
)

SEED = 31337


def miscalibrated(n: int = 6_000, seed: int = SEED) -> tuple[list[str], list[float], list[int]]:
    """Scores that rank correctly and lie about their own probability.

    A true probability `t` is drawn, the label is sampled from it, and the
    reported score is pushed toward the extremes - which is exactly what the
    conditional-independence assumption does to a Fellegi-Sunter posterior.
    """
    rng = random.Random(seed)
    keys, scores, labels = [], [], []
    for i in range(n):
        t = rng.random()
        keys.append(f"R{i:06d}")
        scores.append(t**0.4 if t > 0.5 else t**2.5)
        labels.append(1 if rng.random() < t else 0)
    return keys, scores, labels


# --------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_holdout_split_is_stable_and_roughly_the_right_size() -> None:
    keys = [f"R{i}" for i in range(20_000)]
    first = [in_holdout(k, SEED) for k in keys]
    second = [in_holdout(k, SEED) for k in keys]
    assert first == second
    assert sum(first) / len(first) == pytest.approx(DEFAULT_HOLDOUT, abs=0.02)


@pytest.mark.unit
def test_holdout_membership_survives_the_dataset_changing_around_it() -> None:
    """A record must not change sides because other records were added."""
    assert in_holdout("R42", SEED) == in_holdout("R42", SEED)
    # A different seed is a different split, which is the point of seeding it.
    flips = sum(in_holdout(f"R{i}", 1) != in_holdout(f"R{i}", 2) for i in range(2_000))
    assert flips > 0


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_perfect_calibration_scores_zero_ece() -> None:
    predicted = [0.05] * 100 + [0.95] * 100
    actual = [0] * 95 + [1] * 5 + [0] * 5 + [1] * 95
    metrics = reliability(predicted, actual)
    assert metrics.ece == pytest.approx(0.0, abs=1e-9)
    assert metrics.brier == pytest.approx(0.0475, abs=1e-6)


@pytest.mark.unit
def test_overconfidence_is_visible_in_ece_and_mce() -> None:
    predicted = [0.99] * 200
    actual = [1] * 100 + [0] * 100
    metrics = reliability(predicted, actual)
    assert metrics.ece == pytest.approx(0.49, abs=0.01)
    assert metrics.mce == pytest.approx(0.49, abs=0.01)


@pytest.mark.unit
def test_bins_cover_the_unit_interval_and_the_top_edge_lands_in_the_last_bin() -> None:
    metrics = reliability([0.0, 1.0], [0, 1], bins=10)
    assert len(metrics.bins) == 10
    assert metrics.bins[0].count == 1
    assert metrics.bins[-1].count == 1
    assert sum(b.count for b in metrics.bins) == 2


@pytest.mark.unit
def test_empty_input_is_not_an_error() -> None:
    metrics = reliability([], [])
    assert metrics.n == 0 and metrics.ece == 0.0
    assert log_loss([], []) == 0.0


@pytest.mark.unit
def test_mismatched_lengths_raise() -> None:
    with pytest.raises(ValueError, match="same length"):
        reliability([0.1, 0.2], [1])


# --------------------------------------------------------------------------
# isotonic regression
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_isotonic_is_monotone_non_decreasing() -> None:
    _keys, scores, labels = miscalibrated()
    calibrator = fit_isotonic(scores, labels)
    assert all(b >= a for a, b in pairwise(calibrator.y))
    assert all(b >= a for a, b in pairwise(calibrator.x))
    values = [calibrator.apply(x / 100) for x in range(101)]
    assert all(b >= a - 1e-12 for a, b in pairwise(values))


@pytest.mark.unit
def test_isotonic_clamps_outside_its_fitted_range() -> None:
    calibrator = IsotonicCalibrator((0.2, 0.8), (0.1, 0.9))
    assert calibrator.apply(-5.0) == pytest.approx(0.1)
    assert calibrator.apply(5.0) == pytest.approx(0.9)
    assert calibrator.apply(0.5) == pytest.approx(0.5)


@pytest.mark.unit
def test_isotonic_round_trips_through_json_shaped_dicts() -> None:
    """Applying a calibrator at inference must need no fitting code at all."""
    _keys, scores, labels = miscalibrated()
    calibrator = fit_isotonic(scores, labels)
    restored = IsotonicCalibrator.from_dict(calibrator.as_dict())
    for probe in (0.0, 0.13, 0.5, 0.87, 1.0):
        assert restored.apply(probe) == pytest.approx(calibrator.apply(probe))


@pytest.mark.unit
def test_resolution_reduces_the_knot_count() -> None:
    """The regularizer that stops PAVA planting a step on a coincidence."""
    _keys, scores, labels = miscalibrated()
    fine = fit_isotonic(scores, labels)
    coarse = fit_isotonic(scores, labels, resolution=0.05)
    assert len(coarse.x) < len(fine.x)


@pytest.mark.unit
def test_identity_and_passthrough_do_what_they_say() -> None:
    assert IsotonicCalibrator.identity().apply(0.42) == pytest.approx(0.42)
    # Passthrough has to survive the match-weight range, which leaves [0, 1].
    assert IsotonicCalibrator.passthrough().apply(-37.5) == pytest.approx(-37.5)
    assert IsotonicCalibrator.passthrough().apply(86.0) == pytest.approx(86.0)


# --------------------------------------------------------------------------
# thresholds
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_accept_threshold_is_the_lowest_cut_that_meets_the_target() -> None:
    """Lowest, not safest: a higher cut reviews more work for no extra precision."""
    confidence = [0.99, 0.95, 0.90, 0.85, 0.40, 0.10]
    actual = [1, 1, 1, 1, 0, 0]
    thresholds = choose_thresholds(confidence, actual, target_precision=0.99)
    assert thresholds.t_auto_accept == pytest.approx(0.85)
    assert thresholds.precision_target_met


@pytest.mark.unit
def test_reject_threshold_discards_the_group_it_names() -> None:
    """The scorer rejects on `confidence < t`, so the cut sits above the group.

    Leaving it *at* the discarded value strands every one of those records in
    the grey band - which, for a model whose negatives all land on one score,
    is every negative it had.
    """
    confidence = [0.9, 0.9, 0.9, 0.0, 0.0]
    actual = [1, 1, 1, 0, 0]
    thresholds = choose_thresholds(confidence, actual, target_recall=0.99)
    assert thresholds.t_auto_reject > 0.0
    assert thresholds.grey_band_fraction == pytest.approx(0.0)


@pytest.mark.unit
def test_unreachable_precision_target_is_reported_not_faked() -> None:
    confidence = [0.9] * 50 + [0.1] * 50
    actual = [1, 0] * 25 + [0] * 50
    thresholds = choose_thresholds(confidence, actual, target_precision=0.99)
    assert not thresholds.precision_target_met
    assert thresholds.achieved_precision < 0.99
    assert thresholds.t_auto_accept == pytest.approx(1.0)


@pytest.mark.unit
def test_grey_band_is_the_volume_between_the_two_cuts() -> None:
    confidence = [0.95, 0.80, 0.60, 0.40, 0.05]
    actual = [1, 1, 1, 0, 0]
    thresholds = choose_thresholds(confidence, actual, 0.99, 0.60)
    counted = sum(1 for c in confidence if thresholds.t_auto_reject <= c < thresholds.t_auto_accept)
    assert thresholds.grey_band_fraction == pytest.approx(counted / len(confidence))


@pytest.mark.unit
def test_empty_holdout_yields_an_empty_accept_region() -> None:
    thresholds = choose_thresholds([], [])
    assert thresholds.t_auto_accept == 1.0
    assert not thresholds.precision_target_met


# --------------------------------------------------------------------------
# the whole step
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_calibration_improves_ece_on_held_out_data() -> None:
    keys, scores, labels = miscalibrated()
    result = calibrate(keys, scores, labels, seed=SEED, resolution=None)
    assert result.before.ece > 0.1
    assert result.after.ece < result.before.ece / 3
    assert result.after.brier <= result.before.brier


@pytest.mark.unit
def test_the_calibrator_never_sees_the_holdout() -> None:
    """Everything reported is measured on data the fit did not touch."""
    keys, scores, labels = miscalibrated()
    result = calibrate(keys, scores, labels, seed=SEED)
    assert result.n_fit + result.n_holdout == len(keys)
    assert result.n_holdout / len(keys) == pytest.approx(DEFAULT_HOLDOUT, abs=0.03)


@pytest.mark.unit
def test_calibration_is_deterministic() -> None:
    keys, scores, labels = miscalibrated()
    first = calibrate(keys, scores, labels, seed=SEED).as_dict()
    second = calibrate(keys, scores, labels, seed=SEED).as_dict()
    assert first == second


@pytest.mark.unit
def test_a_holdout_with_no_negatives_is_flagged_rather_than_trusted() -> None:
    """Perfect precision at every threshold is not evidence of anything."""
    keys = [f"R{i}" for i in range(400)]
    scores = [0.5 + i / 1000 for i in range(400)]
    labels = [1] * 400
    result = calibrate(keys, scores, labels, seed=SEED)
    assert any("negative" in note for note in result.notes)


@pytest.mark.unit
def test_raw_probabilities_drive_the_before_metrics() -> None:
    """The calibrator is fitted on weights; the "before" curve is still a curve.

    Without the separation the before-and-after comparison would bin raw match
    weights as though they were probabilities, which is not a chart.
    """
    keys, probabilities, labels = miscalibrated()
    weights = [(p - 0.5) * 60 for p in probabilities]  # a monotone log-odds-ish scale
    result = calibrate(
        keys, weights, labels, seed=SEED, raw_probabilities=probabilities, resolution=None
    )
    assert 0.0 <= result.before.ece <= 1.0
    assert result.after.ece < result.before.ece
    assert all(0.0 <= b.mean_predicted <= 1.0 for b in result.after.bins)
