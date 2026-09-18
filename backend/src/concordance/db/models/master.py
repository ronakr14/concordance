"""Master data: providers, uploaded sanction files, and the rows inside them.

Two design points are worth stating here rather than discovering later.

**Normalized columns are persisted, not computed on read.** `name_norm` and the
rest are written at load time by the Stage 2 functions - the same functions the
in-memory path uses, called once. Recomputing them in SQL would be a second
implementation of normalization, and the two would drift on the first nickname
added to the reference data.

**Block keys live in their own table.** `provider_block_keys` holds one row per
(provider, block, key) pair, produced by the same `_keys()` function the
in-memory generator indexes with. That is what lets `SqlCandidateGenerator` be a
single indexed query per block and still return the candidate set the in-memory
generator returns - the keys are not re-derived in SQL, they are looked up.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from concordance.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from concordance.db.enums import (
    ProviderStatus,
    SanctionFileStatus,
    check_values,
)


class NormalizedColumnsMixin:
    """The five normalized columns, mirrored on providers and sanction records.

    `name_phonetic` holds the record's primary phonetic key. A record usually
    has more than one - a double-metaphone alternate, a nickname's key - and all
    of them are indexed in `provider_block_keys`. This column exists for the
    single-key lookups and for a human reading the table.
    """

    name_norm: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    name_sorted_norm: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    name_phonetic: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    addr_norm: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")
    zip5: Mapped[str] = mapped_column(String(5), nullable=False, server_default="")
    #: `name_sorted_norm` for a person, `org_name_norm` for an organization,
    #: spaces removed - the exact string the trigram block compares. Stored
    #: rather than derived so the pg_trgm index and the in-memory index see
    #: identical input.
    trigram_key: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")


class IdentityColumnsMixin:
    """The source fields shared by a provider and a sanction record."""

    npi: Mapped[str | None] = mapped_column(String(20))
    first_name: Mapped[str | None] = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    suffix: Mapped[str | None] = mapped_column(String(20))
    dob: Mapped[date | None] = mapped_column(Date)
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(2))
    zip: Mapped[str | None] = mapped_column(String(10))
    license_number: Mapped[str | None] = mapped_column(String(50))
    license_state: Mapped[str | None] = mapped_column(String(2))
    specialty: Mapped[str | None] = mapped_column(String(100))
    organization_name: Mapped[str | None] = mapped_column(String(200))
    dba_name: Mapped[str | None] = mapped_column(String(200))
    ein: Mapped[str | None] = mapped_column(String(20))
    is_organization: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class Provider(
    UUIDPrimaryKeyMixin, IdentityColumnsMixin, NormalizedColumnsMixin, CreatedAtMixin, Base
):
    """The provider master. Read-heavy, written only by the loader."""

    __tablename__ = "providers"

    #: The business key - `P0000123` in the generated data, the source system's
    #: id in production. Unique, and what every other table references.
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=ProviderStatus.ACTIVE
    )
    #: Load order. The in-memory generator breaks candidate ties by the order
    #: providers were indexed in; without a stored equivalent the SQL generator
    #: could return the same set in a different order and fail the GATE 5
    #: equivalence check for a reason that is not a difference in blocking.
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Synthetic-data provenance: which generated cluster this provider belongs
    #: to and what role it plays in it. Null for real production data.
    cluster_id: Mapped[str | None] = mapped_column(String(64))
    cluster_role: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        check_values("status", ProviderStatus),
        Index("uq_providers_provider_id", "provider_id", unique=True),
        Index("ix_providers_npi", "npi"),
        Index("ix_providers_name_norm", "name_norm"),
        Index("ix_providers_state_dob", "state", "dob"),
        Index("ix_providers_name_phonetic_state", "name_phonetic", "state"),
        Index("ix_providers_license_number_license_state", "license_number", "license_state"),
        Index("ix_providers_zip5_last_name", "zip5", "last_name"),
        Index("ix_providers_ordinal", "ordinal"),
        Index(
            "ix_providers_trigram_key_gin",
            "trigram_key",
            postgresql_using="gin",
            postgresql_ops={"trigram_key": "gin_trgm_ops"},
        ),
    )


class ProviderBlockKey(Base):
    """One (provider, block, key) pair. The blocking index, as a table.

    Written by the loader from `blocking._keys()`, so the SQL generator and the
    in-memory generator agree by construction rather than by two people
    implementing the same rule twice. The trigram block is not stored here - it
    is a similarity search, not a key lookup, and it has its own GIN index.
    """

    __tablename__ = "provider_block_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("providers.provider_id", ondelete="CASCADE"),
        nullable=False,
    )
    block: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Denormalized copy of `providers.ordinal`, so the candidate query can
    #: order by it without joining back to `providers`.
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_provider_block_keys_block_key", "block", "key"),
        Index("ix_provider_block_keys_provider_id", "provider_id"),
    )


class ColumnMapping(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """How one authority's column headers map onto the canonical field set (Q1).

    Persisted per source so the second upload from the same authority needs no
    mapping step, and versioned by name so a source that changes its layout does
    not silently remap the old one.
    """

    __tablename__ = "column_mappings"

    source_authority: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: `{canonical_field: source_header}`. Canonical fields are
    #: `sanctions.CANONICAL_FIELDS`; anything else is rejected on write.
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("uq_column_mappings_source_authority_name", "source_authority", "name", unique=True),
    )


class SanctionFile(UUIDPrimaryKeyMixin, Base):
    """An uploaded file, in one of the two-phase upload's three states (Q1)."""

    __tablename__ = "sanction_files"

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Content hash of the uploaded bytes. Unique, so the same file cannot be
    #: committed twice and produce two runs that disagree about the same data.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    mapping_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("column_mappings.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=SanctionFileStatus.INSPECTED
    )
    source_authority: Mapped[str | None] = mapped_column(String(100))
    #: Why a `REJECTED` file was rejected, or what the inspection found.
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        check_values("status", SanctionFileStatus),
        Index("uq_sanction_files_sha256", "sha256", unique=True),
        Index("ix_sanction_files_status", "status"),
    )


class SanctionRecord(
    UUIDPrimaryKeyMixin, IdentityColumnsMixin, NormalizedColumnsMixin, CreatedAtMixin, Base
):
    """One row of one uploaded file, extracted into the canonical fields.

    `raw` keeps the original row verbatim. That is not redundancy: a match is a
    statement about a document someone sent us, and defending it later means
    showing the document as it arrived, not as our parser understood it.
    """

    __tablename__ = "sanction_records"

    #: The business key. `S000123` in the generated data.
    record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("sanction_files.id", ondelete="CASCADE")
    )
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    #: The date of birth exactly as the file wrote it. Sanction files carry
    #: partial and malformed dates - `08-24-57`, `May 26, 1985`, `20000326` -
    #: and which of those a record has is evidence the matcher uses. The typed
    #: `dob` column above is the parsed reading, NULL where no reading is
    #: possible; this column is what the domain record and the snapshot hash
    #: read, so that Postgres and Parquet describe the same record.
    dob_raw: Mapped[str | None] = mapped_column(String(64))
    sanction_type: Mapped[str | None] = mapped_column(String(100))
    exclusion_date: Mapped[date | None] = mapped_column(Date)
    reinstatement_date: Mapped[date | None] = mapped_column(Date)
    source_authority: Mapped[str | None] = mapped_column(String(100))
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: Versioning. A record's identity is `(source_authority, record_id)`, and
    #: an upload that changes a known record inserts a new row rather than
    #: editing this one: the old row is what earlier runs were decided against,
    #: and replaying them needs it exactly as it was. Exactly one version per
    #: identity is current, which the partial unique index below enforces.
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("sanction_records.id", ondelete="SET NULL")
    )
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_sanction_records_record_id", "record_id"),
        Index(
            "uq_sanction_records_current_identity",
            text("coalesce(source_authority, '')"),
            "record_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_sanction_records_current_ordinal",
            "ordinal",
            postgresql_where=text("is_current"),
        ),
        Index("uq_sanction_records_file_id_record_id", "file_id", "record_id", unique=True),
        Index("ix_sanction_records_sanction_type", "sanction_type"),
        Index("ix_sanction_records_file_id", "file_id"),
        Index("ix_sanction_records_npi", "npi"),
        Index("ix_sanction_records_name_norm", "name_norm"),
        Index("ix_sanction_records_state_dob", "state", "dob"),
        Index("ix_sanction_records_name_phonetic_state", "name_phonetic", "state"),
        Index(
            "ix_sanction_records_license_number_license_state", "license_number", "license_state"
        ),
        Index("ix_sanction_records_zip5_last_name", "zip5", "last_name"),
        Index("ix_sanction_records_ordinal", "ordinal"),
        Index(
            "ix_sanction_records_trigram_key_gin",
            "trigram_key",
            postgresql_using="gin",
            postgresql_ops={"trigram_key": "gin_trgm_ops"},
        ),
    )


__all__ = [
    "ColumnMapping",
    "IdentityColumnsMixin",
    "NormalizedColumnsMixin",
    "Provider",
    "ProviderBlockKey",
    "SanctionFile",
    "SanctionRecord",
]
