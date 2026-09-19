"""How the LLM experiment splits a stratum's sample across its cells."""

from __future__ import annotations

import pytest

from concordance.eval.llm_experiment import MIN_CELL, _allocate


@pytest.mark.parametrize(
    ("sizes", "n"),
    [([900, 100], 100), ([3, 997], 100), ([0, 500], 100), ([40, 20], 100), ([7, 0], 3), ([1, 1], 1)],
)
def test_parts_sum_to_the_sample_and_never_exceed_a_cell(sizes: list[int], n: int) -> None:
    parts = _allocate(sizes, n)
    assert sum(parts) == min(n, sum(sizes))
    assert all(0 <= p <= s for p, s in zip(parts, sizes, strict=True))


def test_a_small_cell_is_never_left_empty() -> None:
    parts = _allocate([3, 997], 100)
    assert parts[0] == 3
    parts = _allocate([50, 5_000], 100)
    assert parts[0] >= MIN_CELL


def test_the_split_is_proportional_beyond_the_floor() -> None:
    small, large = _allocate([1_000, 3_000], 100)
    assert (small, large) == (25, 75)


def test_nothing_to_sample() -> None:
    assert _allocate([0, 0], 100) == [0, 0]
    assert _allocate([10, 10], 0) == [0, 0]
