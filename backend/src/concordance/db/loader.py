"""Import a generated Parquet dataset into Postgres.

**`COPY`, not the ORM.** 50,000 providers as ORM inserts is 50,000 statements
and several minutes; as a binary `COPY` stream it is one statement. The loader
is run every time the generator changes, so the difference is felt on every
iteration, not once.

**Normalized columns are computed here, by the Stage 2 functions.** The
alternative - deriving them in SQL - would be a second implementation of
normalization, and the two would disagree the first time a nickname is added to
the reference data. The same call that feeds the in-memory index feeds the
table, so a record blocks identically whichever backend is asked.

**Block keys are written alongside.** `provider_block_keys` is the SQL
generator's index, and it comes from the same `_keys()` the in-memory generator
indexes with. Recomputing them at query time would defeat the point of having a
database; deriving them differently would defeat the point of having a seam.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from concordance.logging_setup import get_logger
from concordance.matching.normalization import (
    NormalizedRecord,
    normalize_provider,
    normalize_sanction,
)
from concordance.store.parquet_store import ParquetRecordStore
from concordance.store.sql_candidates import block_keys, trigram_value

log = get_logger("db.loader")

#: Rows per `COPY` flush. Large enough to amortise the round trip, small enough
#: that a failure does not roll back an hour of work.
COPY_BATCH = 10_000

PROVIDER_COLUMNS = (
    "id",
    "provider_id",
    "npi",
    "first_name",
    "middle_name",
    "last_name",
    "suffix",
    "dob",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "zip",
    "license_number",
    "license_state",
    "specialty",
    "organization_name",
    "dba_name",
    "ein",
    "is_organization",
    "status",
    "name_norm",
    "name_sorted_norm",
    "name_phonetic",
    "addr_norm",
    "zip5",
    "trigram_key",
    "ordinal",
    "cluster_id",
    "cluster_role",
)

SANCTION_COLUMNS = (
    "id",
    "record_id",
    "file_id",
    "source_authority",
    "npi",
    "first_name",
    "middle_name",
    "last_name",
    "suffix",
    "dob",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "zip",
    "license_number",
    "license_state",
    "specialty",
    "organization_name",
    "dba_name",
    "ein",
    "is_organization",
    "dob_raw",
    "sanction_type",
    "exclusion_date",
    "reinstatement_date",
    "raw",
    "name_norm",
    "name_sorted_norm",
    "name_phonetic",
    "addr_norm",
    "zip5",
    "trigram_key",
    "ordinal",
)

BLOCK_KEY_COLUMNS = ("provider_id", "block", "key", "ordinal")

GROUND_TRUTH_COLUMNS = (
    "sanction_record_id",
    "expected_provider_id",
    "expected_outcome",
    "corruption_profile",
    "scenario_tag",
)


@dataclass
class LoadReport:
    """What the load did, in numbers worth printing."""

    providers: int = 0
    block_keys: int = 0
    sanctions: int = 0
    ground_truth: int = 0
    seconds: float = 0.0
    stage_seconds: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "providers": self.providers,
            "block_keys": self.block_keys,
            "sanctions": self.sanctions,
            "ground_truth": self.ground_truth,
            "seconds": round(self.seconds, 2),
            "stage_seconds": {k: round(v, 2) for k, v in self.stage_seconds.items()},
        }


def _normalized_columns(normalized: NormalizedRecord) -> tuple[str, str, str, str, str, str]:
    """The six persisted derivations, in table order."""
    address = normalized.address
    name_norm = normalized.org_name_norm if normalized.is_organization else normalized.name_norm
    sorted_norm = (
        normalized.org_name_norm if normalized.is_organization else normalized.name_sorted_norm
    )
    phonetic = normalized.phonetic_keys[0] if normalized.phonetic_keys else ""
    addr = address.line or address.po_box or ""
    return (name_norm, sorted_norm, phonetic, addr, address.zip5, trigram_value(normalized))


def _provider_rows(
    store: ParquetRecordStore,
) -> Iterator[tuple[tuple[Any, ...], list[tuple[Any, ...]]]]:
    """One provider row and its block-key rows, in file order."""
    frame = store.providers_frame
    clusters = (
        {
            str(row["provider_id"]): (row.get("cluster_id"), row.get("cluster_role"))
            for row in frame[["provider_id", "cluster_id", "cluster_role"]].to_dict("records")
        }
        if {"cluster_id", "cluster_role"} <= set(frame.columns)
        else {}
    )

    for ordinal, provider in enumerate(store.all_providers()):
        normalized = normalize_provider(provider)
        name_norm, sorted_norm, phonetic, addr, zip5, trigram = _normalized_columns(normalized)
        cluster_id, cluster_role = clusters.get(provider.provider_id, (None, None))
        row = (
            uuid.uuid4(),
            provider.provider_id,
            provider.npi,
            provider.first_name,
            provider.middle_name,
            provider.last_name,
            provider.suffix,
            provider.dob,
            provider.address_line1,
            provider.address_line2,
            provider.city,
            provider.state,
            provider.zip,
            provider.license_number,
            provider.license_state,
            provider.specialty,
            provider.organization_name,
            provider.dba_name,
            provider.ein,
            provider.is_organization,
            provider.status,
            name_norm,
            sorted_norm,
            phonetic,
            addr,
            zip5,
            trigram,
            ordinal,
            _clean_optional(cluster_id),
            _clean_optional(cluster_role),
        )
        keys = [
            (provider.provider_id, block, key, ordinal)
            for block, key in block_keys(normalized, indexing=True)
        ]
        yield row, keys


def _sanction_rows(store: ParquetRecordStore) -> Iterator[tuple[tuple[Any, ...], str]]:
    """One sanction row, plus the business record id its ground truth is keyed by."""
    for ordinal, record in enumerate(store.all_sanctions()):
        normalized = normalize_sanction(record)
        name_norm, sorted_norm, phonetic, addr, zip5, trigram = _normalized_columns(normalized)
        row_id = uuid.uuid4()
        row = (
            row_id,
            record.record_id,
            None,  # file_id: a generated dataset did not arrive as an upload
            record.source_authority,
            record.npi,
            record.first_name,
            record.middle_name,
            record.last_name,
            record.suffix,
            _as_date(record.dob),
            record.address_line1,
            record.address_line2,
            record.city,
            record.state,
            record.zip,
            record.license_number,
            record.license_state,
            record.specialty,
            record.organization_name,
            record.dba_name,
            record.ein,
            record.is_organization,
            # Verbatim, beside the parsed reading in `dob` above. A sanction
            # file's malformed date is evidence, not noise, and a DATE column
            # cannot hold `08-24-57`.
            record.dob,
            record.sanction_type,
            record.exclusion_date,
            record.reinstatement_date,
            json.dumps(record.raw or {}),
            name_norm,
            sorted_norm,
            phonetic,
            addr,
            zip5,
            trigram,
            ordinal,
        )
        yield row, record.record_id


def _clean_optional(value: Any) -> Any:
    from concordance.store.hashing import is_null

    return None if is_null(value) else value


def _as_date(value: Any) -> Any:
    """A sanction record's DOB is a string because it is frequently partial."""
    from datetime import date, datetime

    if value in (None, ""):
        return None
    if isinstance(value, (date, datetime)):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        # An unparseable or partial date is not an error here: the normalizer
        # reads the original string, and the column is only for indexed lookups.
        return None


def _copy(cursor: Any, table: str, columns: Sequence[str], rows: Sequence[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    cols = ", ".join(columns)
    with cursor.copy(f"COPY {table} ({cols}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    return len(rows)


def load_dataset(engine: Engine, dataset: Path | str, *, truncate: bool = True) -> LoadReport:
    """Import providers, sanction records, block keys and ground truth.

    One transaction: a half-loaded dataset would look like a complete one with
    missing records, and the snapshot hash would quietly describe something that
    was never generated.
    """
    store = ParquetRecordStore(dataset)
    report = LoadReport()
    started = time.perf_counter()

    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        if truncate:
            cursor.execute(
                "TRUNCATE ground_truth, provider_block_keys, sanction_records, providers "
                "RESTART IDENTITY CASCADE"
            )

        mark = time.perf_counter()
        batch: list[tuple[Any, ...]] = []
        keys: list[tuple[Any, ...]] = []
        for row, row_keys in _provider_rows(store):
            batch.append(row)
            keys.extend(row_keys)
            if len(batch) >= COPY_BATCH:
                report.providers += _copy(cursor, "providers", PROVIDER_COLUMNS, batch)
                batch.clear()
                # Block keys carry a foreign key to `providers`, so they may
                # only be written once the providers they name are in. A
                # provider yields several keys, so flushing on the key buffer's
                # own size would run ahead of the provider batch and violate
                # the constraint. The two flushes are one operation.
                report.block_keys += _copy(
                    cursor, "provider_block_keys", BLOCK_KEY_COLUMNS, keys
                )
                keys.clear()
        report.providers += _copy(cursor, "providers", PROVIDER_COLUMNS, batch)
        report.block_keys += _copy(cursor, "provider_block_keys", BLOCK_KEY_COLUMNS, keys)
        report.stage_seconds["providers"] = time.perf_counter() - mark

        mark = time.perf_counter()
        ids_by_record: dict[str, uuid.UUID] = {}
        batch = []
        for row, record_id in _sanction_rows(store):
            ids_by_record[record_id] = row[0]
            batch.append(row)
            if len(batch) >= COPY_BATCH:
                report.sanctions += _copy(cursor, "sanction_records", SANCTION_COLUMNS, batch)
                batch.clear()
        report.sanctions += _copy(cursor, "sanction_records", SANCTION_COLUMNS, batch)
        report.stage_seconds["sanctions"] = time.perf_counter() - mark

        mark = time.perf_counter()
        truth_rows = [
            (
                ids_by_record[record_id],
                truth.expected_provider_id,
                str(truth.expected_outcome),
                json.dumps(truth.corruption_profile or {}),
                truth.scenario_tag or "",
            )
            for record_id, truth in store.ground_truth().items()
            if record_id in ids_by_record
        ]
        report.ground_truth += _copy(cursor, "ground_truth", GROUND_TRUTH_COLUMNS, truth_rows)
        report.stage_seconds["ground_truth"] = time.perf_counter() - mark

        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    report.seconds = time.perf_counter() - started
    log.info("db.load.done", **report.as_dict())
    return report


def analyze(engine: Engine) -> None:
    """`ANALYZE` after a bulk load.

    Postgres plans from statistics, and a table that was empty when it was last
    analysed plans as if it still is - which turns an indexed blocking query
    into a sequential scan and makes the Stage 5 timings meaningless.
    """
    with engine.begin() as conn:
        conn.execute(text("ANALYZE providers, provider_block_keys, sanction_records, ground_truth"))


__all__ = [
    "BLOCK_KEY_COLUMNS",
    "COPY_BATCH",
    "GROUND_TRUTH_COLUMNS",
    "PROVIDER_COLUMNS",
    "SANCTION_COLUMNS",
    "LoadReport",
    "analyze",
    "load_dataset",
]
