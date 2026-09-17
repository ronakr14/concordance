"""The EM fit, checked against data whose answer is known by construction.

Synthetic pattern counts are generated from *chosen* m, u and lambda, so the
test can ask the only question worth asking of an unsupervised fit: did it
recover the parameters it was given? Every guard rail then gets its own case,
because each one exists to stop a specific failure and a guard with no test is
a comment.
"""

from __future__ import annotations

import math
import random
from itertools import pairwise

import pytest

from concordance.matching.comparators import LEVEL_COUNTS, ModelKind
from concordance.matching.fellegi_sunter import (
    DegenerateFitError,
    FellegiSunterModel,
    PatternCounts,
    fit_em,
    logit,
    sigmoid,
)

SEED = 4242


def synthetic(
    kind: ModelKind = ModelKind.INDIVIDUAL,
    lam: float = 0.08,
    n: int = 30_000,
    seed: int = SEED,
) -> tuple[PatternCounts, float]:
    """Pairs drawn from a two-component mixture with a known mixing proportion.

    Matches agree strongly and non-matches disagree strongly, which is the
    structure the model assumes; the question is whether EM finds it without
    being told which pairs are which.
    """
    rng = random.Random(seed)
    sizes = LEVEL_COUNTS[kind]
    vectors = []
    for _ in range(n):
        is_match = rng.random() < lam
        row = []
        for size in sizes:
            # Geometric-ish weights: agreement likely for matches, unlikely
            # otherwise. The exact shape does not matter, the separation does.
            weights = (
                [2**level for level in range(size)]
                if is_match
                else [2 ** (size - 1 - level) for level in range(size)]
            )
            row.append(rng.choices(range(size), weights=weights, k=1)[0])
        vectors.append(tuple(row))
    return PatternCounts.from_vectors(kind, vectors), lam


@pytest.mark.unit
def test_sigmoid_and_logit_round_trip() -> None:
    for p in (1e-9, 0.01, 0.5, 0.9, 1 - 1e-9):
        assert sigmoid(logit(p)) == pytest.approx(p, abs=1e-9)


@pytest.mark.unit
def test_sigmoid_does_not_overflow() -> None:
    assert sigmoid(-10_000) == pytest.approx(0.0)
    assert sigmoid(10_000) == pytest.approx(1.0)


@pytest.mark.unit
def test_pattern_counts_collapse_duplicates_and_are_order_independent() -> None:
    vectors = [(0, 1, 2, 3, 4, 5, 0, 1), (0, 1, 2, 3, 4, 5, 0, 1), (1, 1, 1, 1, 1, 1, 1, 1)]
    forward = PatternCounts.from_vectors(ModelKind.INDIVIDUAL, vectors)
    backward = PatternCounts.from_vectors(ModelKind.INDIVIDUAL, list(reversed(vectors)))
    assert forward.patterns == backward.patterns
    assert forward.counts == backward.counts
    assert forward.total == 3
    assert len(forward.patterns) == 2


@pytest.mark.unit
def test_em_recovers_the_mixing_proportion() -> None:
    data, lam = synthetic(lam=0.08)
    model = fit_em(data, seed=SEED)
    assert model.lam == pytest.approx(lam, abs=0.02)
    assert model.iterations > 1


@pytest.mark.unit
def test_em_is_deterministic_under_a_seed() -> None:
    """The property Stage 6 replay depends on: same seed, identical parameters."""
    data, _ = synthetic()
    first = fit_em(data, seed=SEED).to_dict()
    second = fit_em(data, seed=SEED).to_dict()
    assert first == second


@pytest.mark.unit
def test_a_different_seed_is_allowed_to_differ_but_must_still_converge() -> None:
    data, lam = synthetic()
    a = fit_em(data, seed=1)
    b = fit_em(data, seed=99)
    assert a.lam == pytest.approx(lam, abs=0.03)
    assert b.lam == pytest.approx(lam, abs=0.03)


@pytest.mark.unit
def test_weights_increase_with_agreement() -> None:
    """Monotone by data, not by construction - the fit is free to disagree."""
    data, _ = synthetic()
    weights = fit_em(data, seed=SEED).weights()
    for field_name, row in weights.items():
        assert row[-1] > row[0], field_name
        assert row[-1] > 0 > row[0], field_name


@pytest.mark.unit
def test_a_level_never_observed_contributes_no_evidence() -> None:
    """The smoothing guard.

    With lambda around 0.03 the match component carries a small share of the
    mass, and plain Laplace smoothing inflates `m` far more than `u` - which
    hands a large *positive* weight to a level the fit has never seen. Here the
    top level of the first field is removed from the data entirely; its weight
    must come out at zero, meaning "no evidence", not "strong evidence".
    """
    kind = ModelKind.INDIVIDUAL
    sizes = LEVEL_COUNTS[kind]
    rng = random.Random(7)
    top = sizes[1] - 1
    vectors = []
    for _ in range(20_000):
        is_match = rng.random() < 0.03
        row = [rng.randrange(sizes[0])]
        # Field 1 never takes its top level, in either component.
        row.append(rng.randrange(top))
        row.extend(rng.randrange(size) for size in sizes[2:])
        if is_match:
            row = [max(v, size - 2) for v, size in zip(row, sizes, strict=True)]
            row[1] = min(row[1], top - 1)
        vectors.append(tuple(row))

    model = fit_em(PatternCounts.from_vectors(kind, vectors), seed=SEED)
    unseen = model.weights()[model.fields[1]][top]
    assert unseen == pytest.approx(0.0, abs=0.05)


@pytest.mark.unit
def test_u_floor_bounds_the_weight_of_a_rare_agreement() -> None:
    data, _ = synthetic()
    model = fit_em(data, seed=SEED, u_floor=1e-3)
    ceiling = math.log2(1.0 / 1e-3)
    for row in model.weights().values():
        assert all(abs(w) <= ceiling + 1e-6 for w in row)


@pytest.mark.unit
def test_degenerate_fit_raises_rather_than_serving_a_flat_model() -> None:
    """One component is not a mixture, and a silent downgrade is the worst case."""
    kind = ModelKind.INDIVIDUAL
    sizes = LEVEL_COUNTS[kind]
    identical = [tuple(size - 1 for size in sizes)] * 5_000
    with pytest.raises(DegenerateFitError):
        fit_em(PatternCounts.from_vectors(kind, identical), seed=SEED)


@pytest.mark.unit
def test_too_few_pairs_is_refused() -> None:
    data, _ = synthetic(n=40)
    with pytest.raises(DegenerateFitError, match="at least"):
        fit_em(data, seed=SEED)


@pytest.mark.unit
def test_label_switching_is_corrected() -> None:
    """EM does not know which component is the match class; the fit decides."""
    data, _lam = synthetic(lam=0.08)
    model = fit_em(data, seed=SEED)
    # The match component must be the agreeing one, not the disagreeing one.
    for field_name in model.fields:
        assert model.m[field_name][-1] > model.u[field_name][-1], field_name
    assert model.lam < 0.5


@pytest.mark.unit
def test_score_matches_the_hand_computed_formula() -> None:
    data, _ = synthetic()
    model = fit_em(data, seed=SEED)
    vector = tuple(size - 1 for size in LEVEL_COUNTS[ModelKind.INDIVIDUAL])
    score = model.score(vector)

    expected_weight = sum(
        math.log2(model.m[name][level] / model.u[name][level])
        for name, level in zip(model.fields, vector, strict=True)
    )
    assert score.match_weight == pytest.approx(expected_weight, rel=1e-9)
    assert score.posterior == pytest.approx(
        sigmoid(expected_weight * math.log(2) + logit(model.lam)), rel=1e-9
    )
    assert [c.field for c in score.contributions] == list(model.fields)
    assert sum(c.weight for c in score.contributions) == pytest.approx(score.match_weight)


@pytest.mark.unit
def test_serialization_round_trips_losslessly() -> None:
    """The config JSON at Stage 5 is this, unchanged - no reshaping on load."""
    data, _ = synthetic()
    model = fit_em(data, seed=SEED)
    restored = FellegiSunterModel.from_dict(model.to_dict())
    assert restored.to_dict() == model.to_dict()
    vector = tuple(size - 1 for size in LEVEL_COUNTS[ModelKind.INDIVIDUAL])
    assert restored.score(vector).match_weight == pytest.approx(model.score(vector).match_weight)


@pytest.mark.unit
def test_convergence_trace_is_recorded_and_monotone() -> None:
    data, _ = synthetic()
    model = fit_em(data, seed=SEED)
    assert model.convergence
    likelihoods = [row["log_likelihood"] for row in model.convergence]
    # EM never decreases the likelihood; a decrease means the M-step is wrong.
    assert all(b >= a - 1e-6 for a, b in pairwise(likelihoods))
    assert model.convergence[-1]["iteration"] == model.iterations


@pytest.mark.unit
def test_organization_model_fits_on_its_own_vector_shape() -> None:
    data, lam = synthetic(kind=ModelKind.ORGANIZATION, n=12_000)
    model = fit_em(data, seed=SEED)
    assert model.fields == tuple(model.m)
    assert len(model.fields) == len(LEVEL_COUNTS[ModelKind.ORGANIZATION])
    assert model.lam == pytest.approx(lam, abs=0.03)
