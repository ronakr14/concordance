"""The engine reconciles 5,000 sanction records against 50,000 providers inside a budget.

What this times is the engine: normalize, index, block, compare and score, in
process, from the Parquet dataset. It deliberately does not time a run through
Postgres. That number depends on the link to the hosted database far more than
on the code (see CHECKLIST GATE 6), so a budget on it would fail on a slow day
and pass on a regression.

Opt-in, because it needs the full generated dataset and a fitted config, and
takes about a minute: `CONCORDANCE_PERF_TESTS=1 python -m pytest tests/perf`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from concordance.eval.pairs import prepare
from concordance.matching.scoring_config import ScoringConfig
from concordance.matching.strategies import build_strategy

pytestmark = [pytest.mark.perf, pytest.mark.slow]

_REPO = Path(__file__).resolve().parents[2]
DATASET = _REPO / "data" / "corruption-0.5"
CONFIG = _REPO / "data" / "configs" / "config_c0.50_s20260914.json"

#: Seconds for the whole 50k x 5k run on the development machine, with headroom.
#: Measured on 2026-09-21 at 124 s (25 ms a record), twice running. The budget is twice that,
#: so a slower machine passes and a change that doubles the cost does not.
BUDGET_SECONDS = 250.0


@pytest.fixture(scope="module")
def inputs() -> tuple[Path, Path]:
    if os.environ.get("CONCORDANCE_PERF_TESTS") != "1":
        pytest.skip("set CONCORDANCE_PERF_TESTS=1 to run the performance budget")
    if not (DATASET / "manifest.json").exists() or not CONFIG.exists():
        pytest.skip(f"needs the generated dataset at {DATASET} and a fitted config at {CONFIG}")
    return DATASET, CONFIG


def test_5k_records_against_50k_providers_stay_inside_the_budget(
    inputs: tuple[Path, Path],
) -> None:
    dataset, config = inputs
    started = time.perf_counter()
    prepared = prepare(dataset, show_progress=False)
    strategy = build_strategy("probabilistic", ScoringConfig.read(config).engine(), None)
    decided = [strategy.decide(work.normalized, work.candidates) for work in prepared]
    elapsed = time.perf_counter() - started

    assert prepared.provider_count == 50_000
    assert len(decided) == 5_000
    print(f"\n50k x 5k in {elapsed:.1f}s ({1000 * elapsed / len(decided):.1f} ms/record)")
    assert elapsed < BUDGET_SECONDS, f"{elapsed:.1f}s against a budget of {BUDGET_SECONDS}s"
