"""Stage 1 end to end: seed a dataset, read it back through the store."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from concordance.domain import Outcome, Provider, SanctionRecord
from concordance.protocols import RecordStore
from concordance.store.parquet_store import ParquetRecordStore
from concordance.synth.excel import DIALECTS
from concordance.synth.inspect import inspect_dataset, verify_dataset
from concordance.synth.pipeline import seed_dataset

PROVIDERS = 1200
SANCTIONS = 300


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dataset")
    seed_dataset(
        providers=PROVIDERS, sanctions=SANCTIONS, corruption=0.5, seed=4242, out_dir=out
    )
    return out


@pytest.mark.integration
def test_manifest_records_everything_needed_to_reproduce(dataset: Path) -> None:
    manifest = json.loads((dataset / "manifest.json").read_text())
    assert manifest["seed"] == 4242
    assert manifest["corruption_level"] == 0.5
    assert manifest["counts"]["providers"] == PROVIDERS
    assert manifest["counts"]["sanction_records"] == SANCTIONS
    assert manifest["counts"]["ground_truth"] == SANCTIONS
    assert manifest["generator_version"]
    assert len(manifest["content_hash"]) == 64
    assert set(manifest["files"]) == {
        "providers.parquet",
        "sanction_records.parquet",
        "ground_truth.parquet",
    }


@pytest.mark.integration
def test_reseeding_the_same_seed_is_byte_identical(tmp_path: Path) -> None:
    """The gate for this stage: a seed reproduces the dataset exactly."""
    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        seed_dataset(providers=400, sanctions=120, corruption=0.4, seed=77, out_dir=out, write_excel=False)
    for name in ("providers.parquet", "sanction_records.parquet", "ground_truth.parquet"):
        assert _sha(a / name) == _sha(b / name), name
    assert json.loads((a / "manifest.json").read_text())["content_hash"] == json.loads(
        (b / "manifest.json").read_text()
    )["content_hash"]


@pytest.mark.integration
def test_reseeding_is_idempotent_in_place(tmp_path: Path) -> None:
    out = tmp_path / "same"
    first = seed_dataset(providers=400, sanctions=120, corruption=0.4, seed=5, out_dir=out, write_excel=False)
    before = _sha(out / "providers.parquet")
    second = seed_dataset(providers=400, sanctions=120, corruption=0.4, seed=5, out_dir=out, write_excel=False)
    assert first["content_hash"] == second["content_hash"]
    assert _sha(out / "providers.parquet") == before


@pytest.mark.integration
def test_a_different_corruption_level_changes_the_data(tmp_path: Path) -> None:
    low = seed_dataset(providers=400, sanctions=120, corruption=0.1, seed=5, out_dir=tmp_path / "low", write_excel=False)
    high = seed_dataset(providers=400, sanctions=120, corruption=0.8, seed=5, out_dir=tmp_path / "high", write_excel=False)
    assert low["content_hash"] != high["content_hash"]


@pytest.mark.integration
def test_store_satisfies_the_record_store_protocol(dataset: Path) -> None:
    store = ParquetRecordStore(dataset)
    assert isinstance(store, RecordStore)
    typed: RecordStore = store  # mypy checks the structural match at this line
    assert typed.provider_count() == PROVIDERS
    providers = list(typed.all_providers())
    assert len(providers) == PROVIDERS
    assert all(isinstance(p, Provider) for p in providers)
    assert typed.get_provider(providers[0].provider_id) == providers[0]
    assert typed.get_provider("nope") is None


@pytest.mark.integration
def test_snapshot_hash_is_stable_and_order_independent(dataset: Path) -> None:
    store = ParquetRecordStore(dataset)
    first = store.snapshot_hash()
    assert first == ParquetRecordStore(dataset).snapshot_hash()

    shuffled = ParquetRecordStore(dataset)
    shuffled.providers_frame = store.providers_frame.sample(frac=1.0, random_state=1)
    assert shuffled.snapshot_hash() == first


@pytest.mark.integration
def test_sanction_batches_page_through_the_file(dataset: Path) -> None:
    store = ParquetRecordStore(dataset)
    first = store.sanction_batch(0, 50)
    assert len(first) == 50
    assert all(isinstance(r, SanctionRecord) for r in first)
    assert first[0].raw, "the unmapped source columns are preserved"
    assert store.sanction_batch(SANCTIONS, 50) == []

    seen = []
    for offset in range(0, SANCTIONS, 100):
        seen.extend(r.record_id for r in store.sanction_batch(offset, 100))
    assert len(set(seen)) == SANCTIONS


@pytest.mark.integration
def test_ground_truth_covers_every_record_exactly_once(dataset: Path) -> None:
    store = ParquetRecordStore(dataset)
    truth = store.ground_truth()
    assert len(truth) == SANCTIONS
    for record in store.all_sanctions():
        gt = truth[record.record_id]
        assert isinstance(gt.expected_outcome, Outcome)
        if gt.expected_outcome is Outcome.MATCH:
            assert gt.expected_provider_id
            assert store.get_provider(gt.expected_provider_id) is not None
        else:
            assert gt.scenario_tag


@pytest.mark.integration
def test_verify_passes_on_a_freshly_seeded_dataset(dataset: Path) -> None:
    report = verify_dataset(dataset)
    assert report.ok, "\n".join(report.lines)


@pytest.mark.integration
def test_inspect_shows_records_beside_their_corruption_profile(dataset: Path) -> None:
    lines = inspect_dataset(dataset, scenario="name_variation", limit=3)
    text = "\n".join(lines)
    assert "scenario=name_variation" in text
    assert "sanction :" in text and "provider :" in text


@pytest.mark.integration
def test_excel_export_uses_non_canonical_headers(dataset: Path) -> None:
    """PLAN 11.1: the mapping path must be exercised from day one."""
    files = sorted((dataset / "excel").glob("*.xlsx"))
    assert len(files) >= 2, "mapping reuse per source needs at least two dialects"

    seen_headers = []
    for path in files:
        headers = [c.value for c in next(load_workbook(path, read_only=True).active.rows)]
        seen_headers.append(tuple(headers))
        assert "first_name" not in headers and "npi" not in headers
        assert any(h in headers for h in ("NPI", "Provider Identifier", "npi_num", "National Provider ID"))
        assert "Source Notes" in headers  # a column no mapping claims
    assert len(set(seen_headers)) >= 2, "dialects must actually differ"


@pytest.mark.integration
def test_excel_export_includes_rows_an_importer_must_reject(dataset: Path) -> None:
    path = sorted((dataset / "excel").glob("*.xlsx"))[0]
    rows = list(load_workbook(path, read_only=True).active.rows)
    width = len(rows[0])
    malformed = [r for r in rows[1:] if len([c for c in r if c.value is not None]) not in (0, width)]
    assert malformed, "upload validation needs something to reject"


@pytest.mark.integration
def test_every_dialect_is_reachable_from_the_reference_table() -> None:
    from concordance.synth.reference import load_reference

    dialects = {d for _, d in load_reference().source_authorities}
    assert dialects <= set(DIALECTS)
