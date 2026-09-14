"""Seeding a complete dataset: providers, sanction records, ground truth.

Output is Parquet plus a `manifest.json`. The manifest deliberately records no
wall-clock time: the gate for this stage is that the same seed reproduces
byte-identical files, and a timestamp would quietly break that. Provenance
comes from the seed, the generator version and the content hashes instead.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from concordance.logging_setup import Progress, get_logger
from concordance.synth.corruption import CorruptionEngine, op_catalogue
from concordance.synth.entities import generate_entities
from concordance.synth.excel import write_sanction_workbooks
from concordance.synth.reference import load_reference, reference_fingerprint
from concordance.synth.sanctions import SCENARIOS, SanctionGenerator

GENERATOR_VERSION = "1.0.0"

log = get_logger("synth")

PROVIDER_FIELDS = [
    "provider_id", "npi", "first_name", "middle_name", "last_name", "suffix", "dob",
    "address_line1", "address_line2", "city", "state", "zip",
    "license_number", "license_state", "specialty",
    "organization_name", "dba_name", "ein", "is_organization", "status",
]

SANCTION_FIELDS = [
    "record_id", "source_authority", "npi", "first_name", "middle_name", "last_name",
    "suffix", "dob", "address_line1", "address_line2", "city", "state", "zip",
    "license_number", "license_state", "specialty",
    "organization_name", "dba_name", "ein", "is_organization",
    "sanction_type", "exclusion_date", "reinstatement_date",
]

PROVIDER_SCHEMA = pa.schema(
    [
        (name, pa.date32() if name == "dob" else pa.bool_() if name == "is_organization" else pa.string())
        for name in PROVIDER_FIELDS
    ]
    + [("cluster_id", pa.string()), ("cluster_role", pa.string())]
)

SANCTION_SCHEMA = pa.schema(
    [
        (
            name,
            pa.date32()
            if name in {"exclusion_date", "reinstatement_date"}
            else pa.bool_()
            if name == "is_organization"
            else pa.string(),
        )
        for name in SANCTION_FIELDS
    ]
    + [("raw_json", pa.string())]
)

GROUND_TRUTH_SCHEMA = pa.schema(
    [
        ("sanction_record_id", pa.string()),
        ("expected_outcome", pa.string()),
        ("expected_provider_id", pa.string()),
        ("scenario_tag", pa.string()),
        ("corruption_profile", pa.string()),
    ]
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_parquet(rows: list[dict[str, Any]], schema: pa.Schema, path: Path) -> None:
    """Write with an explicit schema so the file is stable run to run."""
    frame = pd.DataFrame(rows, columns=[f.name for f in schema])
    for field in schema:
        if field.type == pa.date32():
            frame[field.name] = pd.to_datetime(frame[field.name], errors="coerce").dt.date
        elif field.type == pa.bool_():
            frame[field.name] = frame[field.name].astype("boolean")
        else:
            frame[field.name] = frame[field.name].astype("string")
    table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
    pq.write_table(table, path, compression="snappy", version="2.6", write_statistics=False)


def _provider_rows(entities: list[dict[str, Any]], corruption: float, seed: int) -> list[dict[str, Any]]:
    """The provider master: entities seen through a lightly corrupted lens."""
    engine = CorruptionEngine(level=corruption, seed=seed, side="provider")
    rows: list[dict[str, Any]] = []
    with Progress("corrupt.providers", total=len(entities)) as p:
        for entity in entities:
            corrupted, _profile = engine.apply(entity)
            row = {f: corrupted.get(f) for f in PROVIDER_FIELDS}
            row["provider_id"] = entity["provider_id"]  # identity is never corrupted
            row["cluster_id"] = entity.get("_cluster")
            row["cluster_role"] = entity.get("_cluster_role")
            rows.append(row)
            p.tick()
    return rows


def _sanction_rows(build_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for rec in build_records:
        row = {f: rec.get(f) for f in SANCTION_FIELDS}
        # Every source column, including ones no mapping claims (PLAN 11.1).
        row["raw_json"] = json.dumps(
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in rec.items()},
            sort_keys=True,
            default=str,
        )
        rows.append(row)
    return rows


def seed_dataset(
    providers: int,
    sanctions: int,
    corruption: float,
    seed: int,
    out_dir: Path,
    write_excel: bool = True,
) -> dict[str, Any]:
    """Generate and write the dataset. Returns the manifest it wrote."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ref = load_reference()

    log.info("seed.start", providers=providers, sanctions=sanctions, corruption=corruption, seed=seed)

    with Progress("generate.entities", total=providers):
        entities = generate_entities(providers, seed)

    provider_rows = _provider_rows(entities, corruption, seed)

    with Progress("generate.sanctions", total=sanctions):
        build = SanctionGenerator(entities, corruption, seed).build(sanctions)

    _write_parquet(provider_rows, PROVIDER_SCHEMA, out_dir / "providers.parquet")
    _write_parquet(_sanction_rows(build.records), SANCTION_SCHEMA, out_dir / "sanction_records.parquet")
    _write_parquet(
        [
            {
                "sanction_record_id": t["sanction_record_id"],
                "expected_outcome": t["expected_outcome"],
                "expected_provider_id": t["expected_provider_id"],
                "scenario_tag": t["scenario_tag"],
                "corruption_profile": json.dumps(t["corruption_profile"], sort_keys=True, default=str),
            }
            for t in build.truth
        ],
        GROUND_TRUTH_SCHEMA,
        out_dir / "ground_truth.parquet",
    )

    excel_files: list[str] = []
    if write_excel:
        excel_files = [
            str(p.relative_to(out_dir))
            for p in write_sanction_workbooks(build.records, ref.source_authorities, out_dir / "excel")
        ]

    files = {
        name: _sha256_file(out_dir / name)
        for name in ("providers.parquet", "sanction_records.parquet", "ground_truth.parquet")
    }
    content_hash = hashlib.sha256(
        "".join(f"{k}:{v}" for k, v in sorted(files.items())).encode("utf-8")
    ).hexdigest()

    counts = {
        "providers": len(provider_rows),
        "organizations": sum(1 for r in provider_rows if r["is_organization"]),
        "sanction_records": len(build.records),
        "ground_truth": len(build.truth),
    }
    manifest: dict[str, Any] = {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "corruption_level": corruption,
        "counts": counts,
        "scenarios": build.scenario_counts,
        "scenario_expected_outcome": {k: str(v[1]) for k, v in SCENARIOS.items()},
        "corruption_families": sorted({op["family"] for op in op_catalogue()}),
        "reference_tables": reference_fingerprint(),
        "files": files,
        "excel": excel_files,
        "content_hash": content_hash,
        "out_dir": str(out_dir),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    log.info("seed.done", content_hash=content_hash, **counts)
    return manifest
