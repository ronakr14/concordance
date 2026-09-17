"""The Postgres-side seam implementations, tested without a Postgres.

What can be checked offline is the part that actually breaks: that the hash is
defined identically for both backends, that the loader writes the columns the
table declares, and that the SQL generator derives its keys from the engine
rather than from a second implementation of the blocking rules.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from concordance.db.loader import (
    BLOCK_KEY_COLUMNS,
    GROUND_TRUTH_COLUMNS,
    PROVIDER_COLUMNS,
    SANCTION_COLUMNS,
)
from concordance.db.models import Base
from concordance.domain import Provider
from concordance.matching.blocking import InMemoryCandidateGenerator
from concordance.matching.normalization import normalize_provider
from concordance.store.hashing import (
    PROVIDER_HASH_FIELDS,
    SANCTION_HASH_FIELDS,
    canonical,
    combined_hash,
    is_null,
)
from concordance.store.sql_candidates import block_keys, trigram_value

pytestmark = pytest.mark.unit


def a_provider(**overrides: object) -> Provider:
    base = {
        "provider_id": "P0000001",
        "npi": "1376543210",
        "first_name": "Jonathan",
        "last_name": "Okafor",
        "dob": date(1975, 3, 2),
        "address_line1": "12 Mill Road",
        "city": "Columbus",
        "state": "OH",
        "zip": "43201",
        "license_number": "A-426854",
        "license_state": "OH",
    }
    base.update(overrides)
    return Provider(**base)  # type: ignore[arg-type]


# -- the hash is one definition, shared -----------------------------------


def test_the_hash_is_order_independent() -> None:
    """A Postgres plan may return rows in any order; the hash must not care."""
    rows = [
        {"provider_id": "P1", "npi": "1", "status": "ACTIVE"},
        {"provider_id": "P2", "npi": "2", "status": "ACTIVE"},
        {"provider_id": "P3", "npi": "3", "status": "INACTIVE"},
    ]
    fields = ("provider_id", "npi", "status")
    assert combined_hash(rows, fields) == combined_hash(list(reversed(rows)), fields)


def test_the_hash_changes_when_a_value_changes() -> None:
    fields = ("provider_id", "npi")
    before = [{"provider_id": "P1", "npi": "1"}]
    after = [{"provider_id": "P1", "npi": "2"}]
    assert combined_hash(before, fields) != combined_hash(after, fields)


def test_null_and_empty_are_the_same_field_value() -> None:
    """A column absent in Parquet and NULL in Postgres describe the same record."""
    assert canonical(None) == canonical("") == ""
    assert is_null(float("nan")) is True


def test_a_date_hashes_the_same_whether_it_arrives_as_date_or_datetime() -> None:
    """Parquet hands back `datetime`; psycopg hands back `date`."""
    assert canonical(datetime(1975, 3, 2, 13, 45)) == canonical(date(1975, 3, 2))


def test_booleans_hash_as_words_not_as_integers() -> None:
    """Postgres returns `True`; pandas may return `numpy.bool_`. Both must fold."""
    assert canonical(True) == "true"
    assert canonical(False) == "false"


def test_the_hash_field_lists_exist_on_the_tables() -> None:
    """Hashing a column the Postgres store cannot select would fail at runtime."""
    providers = Base.metadata.tables["providers"].columns
    sanctions = Base.metadata.tables["sanction_records"].columns
    for field in PROVIDER_HASH_FIELDS:
        assert field in providers, f"providers has no column {field}"
    for field in SANCTION_HASH_FIELDS:
        assert field in sanctions, f"sanction_records has no column {field}"


# -- the loader writes what the tables declare -----------------------------


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("providers", PROVIDER_COLUMNS),
        ("sanction_records", SANCTION_COLUMNS),
        ("provider_block_keys", BLOCK_KEY_COLUMNS),
        ("ground_truth", GROUND_TRUTH_COLUMNS),
    ],
)
def test_loader_columns_exist_on_the_table(table: str, columns: tuple[str, ...]) -> None:
    """A `COPY` naming a column that does not exist fails 40,000 rows in."""
    declared = Base.metadata.tables[table].columns
    unknown = [c for c in columns if c not in declared]
    assert not unknown, f"{table} has no columns {unknown}"


def test_loader_supplies_every_column_the_table_requires() -> None:
    """Any non-nullable column without a default has to be in the COPY list."""
    for table, columns in (
        ("providers", PROVIDER_COLUMNS),
        ("sanction_records", SANCTION_COLUMNS),
        ("provider_block_keys", BLOCK_KEY_COLUMNS),
        ("ground_truth", GROUND_TRUTH_COLUMNS),
    ):
        required = {
            c.name
            for c in Base.metadata.tables[table].columns
            if not c.nullable and c.server_default is None and c.autoincrement is not True
        }
        assert required <= set(columns), f"{table} missing {sorted(required - set(columns))}"


# -- blocking keys come from the engine, not from a second implementation --


def test_sql_block_keys_are_the_engine_keys() -> None:
    """The adapter must return exactly what the in-memory index stores.

    If these ever diverge, `provider_block_keys` stops being the same index the
    in-memory generator builds and the GATE 5 equivalence check becomes a
    coincidence rather than a guarantee.
    """
    normalized = normalize_provider(a_provider())
    engine_keys = InMemoryCandidateGenerator()._keys(normalized, indexing=True)
    assert block_keys(normalized, indexing=True) == engine_keys


def test_query_keys_widen_the_birth_year_but_index_keys_do_not() -> None:
    """The adjacent-year keys belong on the query side only.

    Indexing them would triple the `state_dob` block for no recall, because the
    query already asks for all three.
    """
    normalized = normalize_provider(a_provider())
    indexed = {k for b, k in block_keys(normalized, indexing=True) if b == "state_dob"}
    queried = {k for b, k in block_keys(normalized, indexing=False) if b == "state_dob"}
    assert indexed == {"OH|1975"}
    assert queried == {"OH|1974", "OH|1975", "OH|1976"}


def test_the_trigram_key_has_no_spaces() -> None:
    """`pg_trgm` pads each word separately; a space would change the trigram set."""
    normalized = normalize_provider(a_provider(first_name="Mary Jane", last_name="Van Dyke"))
    assert " " not in trigram_value(normalized)


def test_the_trigram_key_is_the_sorted_name_for_a_person() -> None:
    normalized = normalize_provider(a_provider())
    assert trigram_value(normalized) == normalized.name_sorted_norm.replace(" ", "")


def test_the_trigram_key_is_the_org_name_for_an_organization() -> None:
    org = a_provider(
        is_organization=True,
        organization_name="Riverside Family Practice Group LLC",
        first_name=None,
        last_name=None,
    )
    normalized = normalize_provider(org)
    assert trigram_value(normalized) == normalized.org_name_norm.replace(" ", "")
