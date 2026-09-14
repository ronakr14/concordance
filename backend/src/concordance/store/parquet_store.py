"""`RecordStore` over the generated Parquet files.

The first implementation of the Stage 0 seam. Stage 5 adds a Postgres one; the
engine cannot tell them apart, and `snapshot_hash()` is defined so that the same
data gives the same hash in either - hash the canonical field values, sort the
per-row digests, hash the result. Order-independent by construction, so a
Postgres query plan reordering rows cannot invalidate a replay.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any

import pandas as pd

from concordance.domain import GroundTruth, Outcome, Provider, SanctionRecord

# The fields the hash covers, in this order. Storage-specific extras - the
# cluster columns, the raw payload - are deliberately excluded so two backends
# holding the same records agree.
PROVIDER_HASH_FIELDS = (
    "provider_id", "npi", "first_name", "middle_name", "last_name", "suffix", "dob",
    "address_line1", "address_line2", "city", "state", "zip",
    "license_number", "license_state", "specialty",
    "organization_name", "dba_name", "ein", "is_organization", "status",
)

SANCTION_HASH_FIELDS = (
    "record_id", "source_authority", "npi", "first_name", "middle_name", "last_name",
    "suffix", "dob", "address_line1", "address_line2", "city", "state", "zip",
    "license_number", "license_state", "specialty",
    "organization_name", "dba_name", "ein", "is_organization",
    "sanction_type", "exclusion_date", "reinstatement_date",
)


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """``DataFrame.to_dict("records")`` types its keys as ``Hashable``."""
    return [{str(k): v for k, v in row.items()} for row in frame.to_dict("records")]


def _clean(value: Any) -> Any:
    """Parquet nulls arrive as NaN/NaT/pd.NA; the domain wants ``None``."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


def _as_str(value: Any) -> str | None:
    v = _clean(value)
    return None if v is None else str(v)


def _as_date(value: Any) -> date | None:
    v = _clean(value)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


def _canonical(value: Any) -> str:
    v = _clean(value)
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _row_digest(row: dict[str, Any], fields: tuple[str, ...]) -> bytes:
    payload = "\x1f".join(_canonical(row.get(f)) for f in fields)
    return hashlib.sha256(payload.encode("utf-8")).digest()


def combined_hash(rows: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> str:
    """Order-independent content hash: sort row digests, then hash them."""
    digests = sorted(_row_digest(r, fields) for r in rows)
    h = hashlib.sha256()
    for d in digests:
        h.update(d)
    return h.hexdigest()


class ParquetRecordStore:
    """Reads `providers.parquet`, `sanction_records.parquet`, `ground_truth.parquet`."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not (self.path / "providers.parquet").exists():
            raise FileNotFoundError(f"no dataset at {self.path} - run `concordance data seed` first")

    # -- frames (lazy, cached) -------------------------------------------
    @cached_property
    def providers_frame(self) -> pd.DataFrame:
        return pd.read_parquet(self.path / "providers.parquet")

    @cached_property
    def sanctions_frame(self) -> pd.DataFrame:
        return pd.read_parquet(self.path / "sanction_records.parquet")

    @cached_property
    def ground_truth_frame(self) -> pd.DataFrame:
        return pd.read_parquet(self.path / "ground_truth.parquet")

    @cached_property
    def manifest(self) -> dict[str, Any]:
        manifest_path = self.path / "manifest.json"
        return json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    @cached_property
    def _by_id(self) -> dict[str, Provider]:
        return {p.provider_id: p for p in self._iter_providers()}

    # -- conversion -------------------------------------------------------
    @staticmethod
    def _to_provider(row: dict[str, Any]) -> Provider:
        return Provider(
            provider_id=str(row["provider_id"]),
            npi=_as_str(row.get("npi")),
            first_name=_as_str(row.get("first_name")),
            middle_name=_as_str(row.get("middle_name")),
            last_name=_as_str(row.get("last_name")),
            suffix=_as_str(row.get("suffix")),
            dob=_as_date(row.get("dob")),
            address_line1=_as_str(row.get("address_line1")),
            address_line2=_as_str(row.get("address_line2")),
            city=_as_str(row.get("city")),
            state=_as_str(row.get("state")),
            zip=_as_str(row.get("zip")),
            license_number=_as_str(row.get("license_number")),
            license_state=_as_str(row.get("license_state")),
            specialty=_as_str(row.get("specialty")),
            organization_name=_as_str(row.get("organization_name")),
            dba_name=_as_str(row.get("dba_name")),
            ein=_as_str(row.get("ein")),
            is_organization=bool(_clean(row.get("is_organization")) or False),
            status=_as_str(row.get("status")) or "ACTIVE",
        )

    @staticmethod
    def _to_sanction(row: dict[str, Any]) -> SanctionRecord:
        raw = row.get("raw_json")
        return SanctionRecord(
            record_id=str(row["record_id"]),
            source_authority=_as_str(row.get("source_authority")),
            npi=_as_str(row.get("npi")),
            first_name=_as_str(row.get("first_name")),
            middle_name=_as_str(row.get("middle_name")),
            last_name=_as_str(row.get("last_name")),
            suffix=_as_str(row.get("suffix")),
            dob=_as_str(row.get("dob")),
            address_line1=_as_str(row.get("address_line1")),
            address_line2=_as_str(row.get("address_line2")),
            city=_as_str(row.get("city")),
            state=_as_str(row.get("state")),
            zip=_as_str(row.get("zip")),
            license_number=_as_str(row.get("license_number")),
            license_state=_as_str(row.get("license_state")),
            specialty=_as_str(row.get("specialty")),
            organization_name=_as_str(row.get("organization_name")),
            dba_name=_as_str(row.get("dba_name")),
            ein=_as_str(row.get("ein")),
            is_organization=bool(_clean(row.get("is_organization")) or False),
            sanction_type=_as_str(row.get("sanction_type")),
            exclusion_date=_as_date(row.get("exclusion_date")),
            reinstatement_date=_as_date(row.get("reinstatement_date")),
            raw=json.loads(raw) if isinstance(raw, str) and raw else {},
        )

    def _iter_providers(self) -> Iterator[Provider]:
        for row in _records(self.providers_frame):
            yield self._to_provider(row)

    # -- RecordStore ------------------------------------------------------
    def all_providers(self) -> Iterable[Provider]:
        return list(self._by_id.values())

    def get_provider(self, pid: str) -> Provider | None:
        return self._by_id.get(pid)

    def sanction_batch(self, offset: int, limit: int) -> list[SanctionRecord]:
        window = self.sanctions_frame.iloc[offset : offset + limit]
        return [self._to_sanction(row) for row in _records(window)]

    def provider_count(self) -> int:
        return len(self.providers_frame)

    def snapshot_hash(self) -> str:
        """Hash of the provider master. Order-independent, backend-independent."""
        return combined_hash(_records(self.providers_frame), PROVIDER_HASH_FIELDS)

    # -- extras used by evaluation ---------------------------------------
    def sanction_count(self) -> int:
        return len(self.sanctions_frame)

    def sanction_snapshot_hash(self) -> str:
        return combined_hash(_records(self.sanctions_frame), SANCTION_HASH_FIELDS)

    def all_sanctions(self) -> list[SanctionRecord]:
        return [self._to_sanction(row) for row in _records(self.sanctions_frame)]

    def ground_truth(self) -> dict[str, GroundTruth]:
        out: dict[str, GroundTruth] = {}
        for row in _records(self.ground_truth_frame):
            profile = row.get("corruption_profile")
            out[str(row["sanction_record_id"])] = GroundTruth(
                sanction_record_id=str(row["sanction_record_id"]),
                expected_outcome=Outcome(str(row["expected_outcome"])),
                expected_provider_id=_as_str(row.get("expected_provider_id")),
                scenario_tag=_as_str(row.get("scenario_tag")) or "",
                corruption_profile=json.loads(profile) if isinstance(profile, str) and profile else {},
            )
        return out
