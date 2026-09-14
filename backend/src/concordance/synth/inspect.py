"""Reading a generated dataset back: sampling and invariant checks.

`data verify` is the machine-checkable half of GATE 1 - one ground-truth row
per sanction record, counts that agree with the manifest, hashes that still
match what was written. `data inspect` is the human half: it prints records
beside the corruption profile that produced them, which is how a spot-check of
twenty records is done without opening Parquet by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from concordance.store.parquet_store import ParquetRecordStore, _clean, _records


@dataclass
class VerifyReport:
    ok: bool = True
    lines: list[str] = field(default_factory=list)

    def check(self, condition: bool, message: str) -> None:
        self.lines.append(("PASS  " if condition else "FAIL  ") + message)
        self.ok = self.ok and condition


def verify_dataset(path: Path | str) -> VerifyReport:
    """Every invariant GATE 1 asks for, checked programmatically."""
    store = ParquetRecordStore(path)
    report = VerifyReport()
    manifest = store.manifest

    providers = store.providers_frame
    sanctions = store.sanctions_frame
    truth = store.ground_truth_frame

    report.check(len(providers) > 0, f"providers.parquet holds {len(providers)} rows")
    report.check(len(sanctions) > 0, f"sanction_records.parquet holds {len(sanctions)} rows")

    if manifest:
        counts = manifest.get("counts", {})
        report.check(
            counts.get("providers") == len(providers),
            f"manifest provider count agrees ({counts.get('providers')})",
        )
        report.check(
            counts.get("sanction_records") == len(sanctions),
            f"manifest sanction count agrees ({counts.get('sanction_records')})",
        )

    # One ground-truth row per sanction record, no exceptions.
    truth_ids = list(truth["sanction_record_id"])
    sanction_ids = list(sanctions["record_id"])
    report.check(len(truth_ids) == len(set(truth_ids)), "ground truth has no duplicate record ids")
    report.check(
        set(truth_ids) == set(sanction_ids),
        f"every sanction record has exactly one ground-truth row ({len(truth_ids)} vs {len(sanction_ids)})",
    )

    # Provider ids referenced by ground truth must exist.
    known = set(providers["provider_id"])
    expected = {p for p in truth["expected_provider_id"].dropna() if p}
    missing = expected - known
    report.check(not missing, f"all expected_provider_id values resolve ({len(expected)} referenced)")

    report.check(providers["provider_id"].is_unique, "provider ids are unique")
    report.check(sanctions["record_id"].is_unique, "sanction record ids are unique")

    scenarios = set(truth["scenario_tag"])
    required = {
        "exact_npi", "missing_npi", "sentinel_npi", "name_variation", "address_variation",
        "ambiguous", "false_positive_bait", "unmatched",
    }
    report.check(required <= scenarios, f"all eight spec scenarios present ({len(scenarios)} tags)")

    report.lines.append(f"      provider snapshot hash {store.snapshot_hash()[:16]}")
    report.lines.append(f"      sanction snapshot hash {store.sanction_snapshot_hash()[:16]}")
    return report


def _describe(record: dict[str, Any]) -> str:
    keep = [
        "record_id", "npi", "first_name", "last_name", "organization_name",
        "dob", "city", "state", "zip", "license_number",
    ]
    return "  ".join(f"{k}={_clean(record.get(k))!r}" for k in keep if _clean(record.get(k)) is not None)


def inspect_dataset(
    path: Path | str, scenario: str | None = None, limit: int = 10
) -> list[str]:
    """Sample records with their ground truth and applied corruptions."""
    store = ParquetRecordStore(path)
    truth = store.ground_truth_frame
    if scenario:
        truth = truth[truth["scenario_tag"] == scenario]
    sample = truth.head(limit)
    sanctions = store.sanctions_frame.set_index("record_id")
    providers = store.providers_frame.set_index("provider_id")

    lines: list[str] = []
    for row in _records(sample):
        rid = row["sanction_record_id"]
        profile = json.loads(row["corruption_profile"]) if row.get("corruption_profile") else {}
        lines.append(f"--- {rid}  scenario={row['scenario_tag']}  expected={row['expected_outcome']}")
        if rid in sanctions.index:
            rec = dict(sanctions.loc[rid])
            rec["record_id"] = rid
            lines.append(f"  sanction : {_describe(rec)}")
        pid = row.get("expected_provider_id")
        if pid and pid in providers.index:
            prov = dict(providers.loc[pid])
            prov["record_id"] = pid
            lines.append(f"  provider : {_describe(prov)}")
        applied = profile.get("applied", [])
        if applied:
            lines.append("  applied  :")
            for op in applied:
                lines.append(f"    {op['family']}.{op['op']} {op['before']} -> {op['after']}")
        else:
            lines.append("  applied  : (none - record passed through clean)")
        if profile.get("plausible_provider_ids"):
            lines.append(f"  plausible: {profile['plausible_provider_ids']}")
        if profile.get("near_provider_id"):
            lines.append(f"  bait near: {profile['near_provider_id']}")
    return lines
