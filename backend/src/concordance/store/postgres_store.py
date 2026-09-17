"""`RecordStore` over Postgres. The second implementation of the Stage 0 seam.

Nothing in `matching/` changes to make this work, which is the whole point of
the seam and the thing GATE 5 checks with a diff.

Two details carry weight:

**`snapshot_hash()` streams, it does not load.** Hashing 50,000 providers by
materialising them as ORM objects would cost more memory than the engine uses to
score them. The query selects exactly the hashed columns and yields mappings,
and `combined_hash` sorts digests rather than rows.

**Ordering is by `ordinal`, not by primary key.** The in-memory generator breaks
candidate ties by the order providers were indexed in, so the SQL side has to
know that order. `ordinal` is written at load time and is the same number the
Parquet row had, which is what makes the two generators comparable at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from concordance.db.models import (
    GroundTruth as GroundTruthRow,
)
from concordance.db.models import (
    Provider as ProviderRow,
)
from concordance.db.models import (
    SanctionRecord as SanctionRow,
)
from concordance.db.retry import with_reconnect
from concordance.domain import GroundTruth, Outcome
from concordance.domain import Provider as DomainProvider
from concordance.domain import SanctionRecord as DomainSanction
from concordance.store.hashing import (
    PROVIDER_HASH_FIELDS,
    SANCTION_HASH_FIELDS,
    combined_hash,
)

#: Batch size for the streaming hash and the batch reader. Large enough that the
#: round trip is amortised, small enough that a 50k table never lands in memory
#: whole.
STREAM_BATCH = 5_000

#: Rows per statement when hashing. Small enough that one statement takes
#: seconds rather than minutes, which is what keeps a hosted database from
#: closing the connection out from under it.
HASH_PAGE = 1_000


def _to_provider(row: Any) -> DomainProvider:
    return DomainProvider(
        provider_id=row.provider_id,
        npi=row.npi,
        first_name=row.first_name,
        middle_name=row.middle_name,
        last_name=row.last_name,
        suffix=row.suffix,
        dob=row.dob,
        address_line1=row.address_line1,
        address_line2=row.address_line2,
        city=row.city,
        state=row.state,
        zip=row.zip,
        license_number=row.license_number,
        license_state=row.license_state,
        specialty=row.specialty,
        organization_name=row.organization_name,
        dba_name=row.dba_name,
        ein=row.ein,
        is_organization=bool(row.is_organization),
        status=row.status or "ACTIVE",
    )


def _to_sanction(row: Any) -> DomainSanction:
    return DomainSanction(
        record_id=row.record_id,
        source_authority=row.source_authority,
        npi=row.npi,
        first_name=row.first_name,
        middle_name=row.middle_name,
        last_name=row.last_name,
        suffix=row.suffix,
        # The domain record keeps the date of birth as written, because a
        # sanction file's DOB is frequently partial and the normalizer is what
        # decides how to read it. `dob_raw` is that verbatim string; falling
        # back to the parsed column covers rows written before it existed.
        dob=row.dob_raw or (row.dob.isoformat() if row.dob else None),
        address_line1=row.address_line1,
        address_line2=row.address_line2,
        city=row.city,
        state=row.state,
        zip=row.zip,
        license_number=row.license_number,
        license_state=row.license_state,
        specialty=row.specialty,
        organization_name=row.organization_name,
        dba_name=row.dba_name,
        ein=row.ein,
        is_organization=bool(row.is_organization),
        sanction_type=row.sanction_type,
        exclusion_date=row.exclusion_date,
        reinstatement_date=row.reinstatement_date,
        raw=dict(row.raw or {}),
    )


class PostgresRecordStore:
    """Reads `providers`, `sanction_records` and `ground_truth`.

    Takes a `Session` and never opens its own - the caller owns the transaction,
    which is what lets a reconciliation run read the same snapshot the run row
    claims it read.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- RecordStore ------------------------------------------------------
    def all_providers(self) -> Iterable[DomainProvider]:
        stmt = select(ProviderRow).order_by(ProviderRow.ordinal)
        return [_to_provider(r) for r in self.session.scalars(stmt)]

    def iter_providers(self) -> Iterator[DomainProvider]:
        """The same rows, streamed. What the blocking build should use."""
        stmt = (
            select(ProviderRow)
            .order_by(ProviderRow.ordinal)
            .execution_options(yield_per=STREAM_BATCH)
        )
        for row in self.session.scalars(stmt):
            yield _to_provider(row)

    def get_provider(self, pid: str) -> DomainProvider | None:
        row = self.session.scalar(select(ProviderRow).where(ProviderRow.provider_id == pid))
        return None if row is None else _to_provider(row)

    def sanction_batch(self, offset: int, limit: int) -> list[DomainSanction]:
        stmt = select(SanctionRow).order_by(SanctionRow.ordinal).offset(offset).limit(limit)
        return [_to_sanction(r) for r in self.session.scalars(stmt)]

    def provider_count(self) -> int:
        from sqlalchemy import func

        return int(self.session.scalar(select(func.count()).select_from(ProviderRow)) or 0)

    def snapshot_hash(self) -> str:
        """Must equal `ParquetRecordStore.snapshot_hash()` for the same data."""
        columns = [getattr(ProviderRow, name) for name in PROVIDER_HASH_FIELDS]
        return combined_hash(
            self._paged(columns, ProviderRow.provider_id), PROVIDER_HASH_FIELDS
        )

    # -- extras used by evaluation ---------------------------------------
    def sanction_count(self) -> int:
        from sqlalchemy import func

        return int(self.session.scalar(select(func.count()).select_from(SanctionRow)) or 0)

    def sanction_snapshot_hash(self) -> str:
        """Must equal `ParquetRecordStore.sanction_snapshot_hash()`.

        `dob` is hashed from `dob_raw` under the name `dob`: Parquet holds the
        string the file wrote, so hashing the parsed DATE column would make two
        faithful copies of one dataset disagree.
        """
        columns = [
            SanctionRow.dob_raw.label("dob")
            if name == "dob"
            else getattr(SanctionRow, name)
            for name in SANCTION_HASH_FIELDS
        ]
        return combined_hash(self._paged(columns, SanctionRow.record_id), SANCTION_HASH_FIELDS)

    def _paged(
        self, columns: list[Any], key: Any, page: int = HASH_PAGE
    ) -> Iterator[Mapping[str, Any]]:
        """The hash inputs, one short statement at a time.

        Keyset pagination rather than a single streamed scan, because a hosted
        database will close a connection that spends minutes feeding one result
        set across a slow link, and a snapshot hash that fails intermittently is
        worse than one that takes ten statements. The hash is order-independent
        by construction - `combined_hash` sorts the per-row digests - so paging
        cannot change the answer.
        """
        after: Any = None
        while True:
            stmt = select(key.label("_key"), *columns).order_by(key).limit(page)
            if after is not None:
                stmt = stmt.where(key > after)
            def read(statement: Any = stmt) -> list[Mapping[str, Any]]:
                return [cast("Mapping[str, Any]", r) for r in self.session.execute(statement).mappings()]

            rows = with_reconnect(self.session, read, what="snapshot_hash page")
            if not rows:
                return
            yield from rows
            after = rows[-1]["_key"]

    def all_sanctions(self) -> list[DomainSanction]:
        stmt = select(SanctionRow).order_by(SanctionRow.ordinal)
        return [_to_sanction(r) for r in self.session.scalars(stmt)]

    def ground_truth(self) -> dict[str, GroundTruth]:
        """Keyed by the *business* record id, as the Parquet store keys it."""
        stmt = select(GroundTruthRow, SanctionRow.record_id).join(
            SanctionRow, SanctionRow.id == GroundTruthRow.sanction_record_id
        )
        out: dict[str, GroundTruth] = {}
        for truth, record_id in self.session.execute(stmt):
            out[record_id] = GroundTruth(
                sanction_record_id=record_id,
                expected_outcome=Outcome(truth.expected_outcome),
                expected_provider_id=truth.expected_provider_id,
                scenario_tag=truth.scenario_tag or "",
                corruption_profile=dict(truth.corruption_profile or {}),
            )
        return out


__all__ = ["STREAM_BATCH", "PostgresRecordStore"]
