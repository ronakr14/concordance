"""`docs/data_dictionary.md` describes the schema that actually exists.

A data dictionary that drifts is worse than none: it is trusted. So this reads
the live schema and the document and fails on any difference - a table, a
column, a nullability, or an enumerated column whose allowed values the
document lists wrongly or not at all. Adding a column now means documenting it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

DOC = Path(__file__).resolve().parents[2] / "docs" / "data_dictionary.md"


def _documented_tables() -> dict[str, dict[str, bool]]:
    """table -> column -> nullable, from the rows under each table heading."""
    tables: dict[str, dict[str, bool]] = {}
    current: str | None = None
    for line in DOC.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^### `(\w+)`", line)
        if heading:
            current = heading.group(1)
            tables[current] = {}
            continue
        if line.startswith("#"):
            current = None
            continue
        row = re.match(r"^\|\s*`(\w+)`\s*\|[^|]*\|\s*([^|]*)\|", line)
        if current and row:
            tables[current][row.group(1)] = row.group(2).strip().lower().startswith("yes")
    return tables


def _documented_enums() -> dict[str, set[str]]:
    """`table.column` -> allowed values, from the enumerations table."""
    section = DOC.read_text(encoding="utf-8").split("## 2. Enumerations", 1)[1].split("\n## ", 1)[0]
    enums: dict[str, set[str]] = {}
    for line in section.splitlines():
        row = re.match(r"^\|\s*`(\w+\.\w+)`\s*\|(.*)\|\s*$", line)
        if row:
            enums[row.group(1)] = set(re.findall(r"`([^`]+)`", row.group(2)))
    return enums


def test_every_table_and_column_is_documented_as_it_is(owner_session) -> None:
    rows = owner_session.execute(
        text(
            """
            SELECT c.table_name, c.column_name, c.is_nullable = 'YES'
            FROM information_schema.columns c
            JOIN pg_tables t ON t.tablename = c.table_name AND t.schemaname = c.table_schema
            WHERE c.table_schema = 'public' AND c.table_name <> 'alembic_version'
            """
        )
    ).all()
    live: dict[str, dict[str, bool]] = {}
    for table, column, nullable in rows:
        live.setdefault(table, {})[column] = nullable

    documented = _documented_tables()
    assert sorted(live) == sorted(documented), "tables differ"
    problems = [
        f"{table}.{column}: {'undocumented' if column not in documented[table] else 'nullable is wrong'}"
        for table, columns in live.items()
        for column, nullable in columns.items()
        if documented[table].get(column) is not nullable
    ]
    problems += [
        f"{table}.{column}: documented but absent"
        for table, columns in documented.items()
        for column in columns
        if column not in live[table]
    ]
    assert problems == []


def test_every_enumerated_column_lists_its_allowed_values(owner_session) -> None:
    rows = owner_session.execute(
        text(
            """
            SELECT conrelid::regclass::text, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE contype = 'c' AND connamespace = 'public'::regnamespace
              AND pg_get_constraintdef(oid) LIKE '%= ANY%'
            """
        )
    ).all()
    live: dict[str, set[str]] = {}
    for table, definition in rows:
        column = re.search(r"\(\((\w+)\)::text = ANY", definition)
        assert column, definition
        live[f"{table}.{column.group(1)}"] = set(re.findall(r"'([^']+)'::character varying", definition))

    assert live == _documented_enums()
