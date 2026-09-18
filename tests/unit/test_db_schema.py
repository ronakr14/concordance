"""Stage 5 schema checks that need no database.

Postgres is required to run a migration; it is not required to know whether the
schema says what it is supposed to say. Everything here compiles the metadata
against the Postgres dialect, reads the migration as text, or compares the
storage vocabulary against the engine's - the class of mistake that would
otherwise be found at `alembic upgrade head` time, on someone else's machine.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from sqlalchemy import create_mock_engine

from concordance.db.enums import (
    Decision,
    EvalStrategy,
    JobStatus,
    ReviewStatus,
    Route,
    RunStatus,
    values,
)
from concordance.db.models import Base

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "backend" / "alembic" / "versions"
DATA_DICTIONARY = REPO_ROOT / "docs" / "data_dictionary.md"


def _ddl() -> str:
    """The `CREATE` statements the metadata produces for Postgres."""
    statements: list[str] = []
    engine = create_mock_engine(
        "postgresql+psycopg://",
        lambda sql, *a, **kw: statements.append(str(sql.compile(dialect=engine.dialect))),
    )
    Base.metadata.create_all(engine, checkfirst=False)
    return "\n".join(statements)


def _migration_source() -> str:
    files = sorted(MIGRATIONS.glob("*.py"))
    assert files, "no migration found - Stage 5 needs an initial revision"
    return "\n".join(f.read_text(encoding="utf-8") for f in files)


# -- the schema compiles ---------------------------------------------------


def test_every_table_compiles_for_postgres() -> None:
    ddl = _ddl()
    for table in Base.metadata.tables:
        assert f"CREATE TABLE {table}" in ddl


def test_every_table_has_a_primary_key() -> None:
    """A table without one cannot be updated or referenced safely."""
    missing = [t.name for t in Base.metadata.tables.values() if not t.primary_key.columns]
    assert not missing, f"tables without a primary key: {missing}"


def test_constraints_are_named() -> None:
    """Autogenerate compares constraint names; unnamed ones produce phantom diffs."""
    unnamed: list[str] = []
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if constraint.name is None or str(constraint.name).startswith("_unnamed_"):
                unnamed.append(f"{table.name}.{type(constraint).__name__}")
        for index in table.indexes:
            if not index.name:
                unnamed.append(f"{table.name}.Index")
    assert not unnamed, f"unnamed constraints or indexes: {unnamed}"


def test_the_trigram_indexes_use_gin_trgm_ops() -> None:
    """The fuzzy block is a similarity search; a btree index would not serve it.

    Three: the blocking key on both sides, and the provider directory's
    substring name search.
    """
    ddl = _ddl()
    assert ddl.count("gin_trgm_ops") == 3, (
        "expected trigram indexes on both trigram keys and on providers.name_norm"
    )
    assert "ix_providers_name_norm_gin ON providers USING gin (name_norm gin_trgm_ops)" in ddl


def test_current_results_have_a_partial_index() -> None:
    """`superseded_by IS NULL` is the default query path (Q5), so it is indexed."""
    assert "WHERE superseded_by IS NULL" in _ddl()


def test_one_active_case_per_provider_and_record() -> None:
    ddl = _ddl()
    assert "uq_cases_active_provider_sanction" in ddl
    assert "WHERE status = 'ACTIVE'" in ddl


# -- the migration ---------------------------------------------------------


def test_the_migration_creates_pg_trgm_before_any_trigram_index() -> None:
    source = _migration_source()
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in source
    assert source.index("CREATE EXTENSION") < source.index("gin_trgm_ops")


def test_the_migration_revokes_write_on_the_audit_log() -> None:
    """An audit trail the application can rewrite is not an audit trail."""
    source = _migration_source()
    assert "REVOKE UPDATE, DELETE ON TABLE audit_logs" in source
    assert "to_regrole" in source, "the revoke must be guarded for databases with no app role"


def test_the_migration_drops_everything_it_creates() -> None:
    """GATE 5 asks `downgrade base` to leave no orphaned objects."""
    source = _migration_source()
    created = set(re.findall(r"op\.create_table\(\s*[\"'](\w+)[\"']", source))
    dropped = set(re.findall(r"op\.drop_table\(\s*[\"'](\w+)[\"']", source))
    assert created == dropped, f"created but never dropped: {sorted(created - dropped)}"
    assert "DROP EXTENSION IF EXISTS pg_trgm" in source


def test_the_migration_covers_every_mapped_table() -> None:
    source = _migration_source()
    created = set(re.findall(r"op\.create_table\(\s*[\"'](\w+)[\"']", source))
    assert set(Base.metadata.tables) == created


def test_the_migration_is_valid_python() -> None:
    for path in sorted(MIGRATIONS.glob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# -- the storage vocabulary matches the engine's ---------------------------


def test_decision_values_match_the_engine_outcome() -> None:
    """`db.enums` restates the engine's vocabulary; a divergence must fail loudly."""
    from concordance.domain import Outcome

    assert set(values(Decision)) == {str(o) for o in Outcome}


def test_route_values_are_a_subset_of_the_engine_routes() -> None:
    from concordance.matching.scorer import Route as EngineRoute

    assert set(values(Route)) <= {str(r) for r in EngineRoute}


def test_strategy_values_match_the_engine_strategies() -> None:
    from concordance.matching.strategies import StrategyName

    assert set(values(EvalStrategy)) == {str(s) for s in StrategyName}


def test_enum_checks_reach_the_ddl() -> None:
    ddl = _ddl()
    for enum in (RunStatus, ReviewStatus, JobStatus):
        for value in values(enum):
            assert f"'{value}'" in ddl


# -- the data dictionary stays true ---------------------------------------


def test_the_data_dictionary_documents_every_column() -> None:
    """The checklist asks for every column; this is what makes that verifiable.

    A column added without a line in `docs/data_dictionary.md` fails here, which
    is the only way a document this size stays accurate for longer than a week.
    """
    text = DATA_DICTIONARY.read_text(encoding="utf-8")
    missing: list[str] = []
    for table in Base.metadata.tables.values():
        section = text.split(f"### `{table.name}`", 1)
        if len(section) == 1:
            missing.append(table.name)
            continue
        body = section[1].split("### `", 1)[0]
        for column in table.columns:
            if f"| `{column.name}` |" not in body:
                missing.append(f"{table.name}.{column.name}")
    assert not missing, f"undocumented: {missing}"


def test_the_data_dictionary_has_no_undocumented_placeholders() -> None:
    assert "_Undocumented._" not in DATA_DICTIONARY.read_text(encoding="utf-8")
