"""The snapshot hash, defined once for every backend.

A run records the hash of both sides of the data it decided against, and a
replay proves it replayed the same input by recomputing them. That only works if
Parquet and Postgres agree exactly, which means the rule has to live in one
place: hash the canonical form of a fixed field list, sort the per-row digests,
hash the sorted digests. Sorting is what makes it order-independent, so a
Postgres query plan that returns rows in a different order than the Parquet file
still produces the same hash.

This module imports nothing outside the standard library on purpose. It is
shared by the pandas-backed store and the SQLAlchemy-backed one, and neither
dependency belongs in the definition of an identity.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

#: The fields the provider hash covers, in this order. Storage-specific
#: extras - the cluster columns, the load ordinal, the surrogate key - are
#: excluded so two backends holding the same records agree.
PROVIDER_HASH_FIELDS = (
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
)

SANCTION_HASH_FIELDS = (
    "record_id",
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
    "sanction_type",
    "exclusion_date",
    "reinstatement_date",
)

#: Pandas' null sentinels, recognised by type name rather than by importing
#: pandas. `NaT` and `NA` are singletons whose truth value raises, so they
#: cannot be tested with `or`, and `isinstance` would need the import this
#: module exists to avoid.
_NULL_TYPE_NAMES = {"NaTType", "NAType"}


def is_null(value: Any) -> bool:
    """`None`, a float NaN, or a pandas null sentinel."""
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN is not equal to itself
        return True
    return type(value).__name__ in _NULL_TYPE_NAMES


def canonical(value: Any) -> str:
    """One field's contribution to a row digest.

    Nulls and empty strings are the same thing here, deliberately: a column that
    is absent in Parquet and NULL in Postgres describes the same record.
    """
    if is_null(value):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def row_digest(row: Mapping[str, Any], fields: tuple[str, ...]) -> bytes:
    payload = "\x1f".join(canonical(row.get(f)) for f in fields)
    return hashlib.sha256(payload.encode("utf-8")).digest()


def combined_hash(rows: Iterable[Mapping[str, Any]], fields: tuple[str, ...]) -> str:
    """Order-independent content hash: sort row digests, then hash them."""
    digests = sorted(row_digest(r, fields) for r in rows)
    h = hashlib.sha256()
    for digest in digests:
        h.update(digest)
    return h.hexdigest()


__all__ = [
    "PROVIDER_HASH_FIELDS",
    "SANCTION_HASH_FIELDS",
    "canonical",
    "combined_hash",
    "is_null",
    "row_digest",
]
