"""Blocking measured against a generated dataset, end to end.

Small by design - the gate numbers come from `match blocking-recall` over the
full 50k file. What is checked here is that the measurement machinery is right
and that recall does not collapse when the corruption dial is turned up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concordance.eval.blocking_recall import measure_blocking_recall, write_report
from concordance.matching.blocking import InMemoryCandidateGenerator
from concordance.protocols import CandidateGenerator, RecordStore
from concordance.store.parquet_store import ParquetRecordStore
from concordance.synth.pipeline import seed_dataset

PROVIDERS = 4000
SANCTIONS = 600


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("blocking")
    seed_dataset(
        providers=PROVIDERS,
        sanctions=SANCTIONS,
        corruption=0.5,
        seed=31337,
        out_dir=out,
        write_excel=False,
    )
    return out


@pytest.mark.integration
def test_generator_consumes_the_store_through_the_protocol(dataset: Path) -> None:
    store: RecordStore = ParquetRecordStore(dataset)
    generator: CandidateGenerator = InMemoryCandidateGenerator(max_candidates=50)
    generator.build(store)
    assert isinstance(generator, CandidateGenerator)

    record = store.sanction_batch(0, 1)[0]
    candidates = generator.candidates(record)
    assert all(store.get_provider(c.provider_id) is not None for c in candidates)


@pytest.mark.integration
def test_recall_clears_the_gate_at_corruption_half(dataset: Path) -> None:
    report = measure_blocking_recall(dataset, max_candidates=50)
    assert report.evaluated > 0
    assert report.recall >= 0.98, report.lines()
    assert report.p95_candidates <= 100
    assert report.mean_candidates <= 100


@pytest.mark.integration
def test_every_block_is_wired_and_contributes(dataset: Path) -> None:
    """A block that never fires is a block that is silently broken."""
    report = measure_blocking_recall(dataset, max_candidates=50)
    for block in ("npi", "state_dob", "phonetic_state", "zip_name3", "license", "trigram"):
        assert report.block_contribution.get(block, 0) > 0, block
    for block in ("ein", "org_token_state", "org_acronym"):
        assert report.block_contribution.get(block, 0) > 0, block


@pytest.mark.integration
def test_candidate_sets_are_reproducible(dataset: Path) -> None:
    store = ParquetRecordStore(dataset)
    records = store.sanction_batch(0, 50)

    def run() -> list[tuple[str, tuple[str, ...]]]:
        generator = InMemoryCandidateGenerator(max_candidates=50)
        generator.build(store)
        return [
            (c.provider_id, c.blocking_keys) for record in records for c in generator.candidates(record)
        ]

    assert run() == run()


@pytest.mark.integration
@pytest.mark.slow
def test_recall_degrades_gracefully_at_the_top_of_the_dial(tmp_path: Path) -> None:
    """At corruption 0.9 recall is expected to fall - it must not fall apart."""
    out = tmp_path / "hard"
    seed_dataset(providers=PROVIDERS, sanctions=SANCTIONS, corruption=0.9, seed=31337, out_dir=out, write_excel=False)
    report = measure_blocking_recall(out, max_candidates=50)
    assert report.recall >= 0.85, report.lines()
    assert report.misses_by_family, "a miss should always name the families that caused it"


@pytest.mark.integration
def test_report_serializes_with_everything_the_gate_needs(dataset: Path, tmp_path: Path) -> None:
    report = measure_blocking_recall(dataset, max_candidates=50, limit=200)
    payload = report.as_dict()
    for key in (
        "recall",
        "mean_candidates",
        "p95_candidates",
        "block_contribution",
        "recall_by_scenario",
        "misses_by_corruption_family",
        "index",
    ):
        assert key in payload
    assert payload["index"]["providers"] == PROVIDERS

    path = write_report(report, tmp_path / "report.json")
    assert path.exists() and path.read_text(encoding="utf-8").strip().startswith("{")
    assert report.lines(), "the human-readable form must not be empty"
