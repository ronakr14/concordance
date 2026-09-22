"""`concordance db reset` empties the database and keeps the migration history.

This is the one test in the suite that destroys data, so it does not run
unless `CONCORDANCE_DESTRUCTIVE_TESTS=1` is set. CI sets it, because CI builds
its database from nothing every run and can afford to lose it; a developer
machine points at a shared hosted database holding a seeded 50,000-provider
dataset, and a test that quietly truncated it would cost an hour of reseeding.
Opt-in is the only safe default here.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CONCORDANCE_DESTRUCTIVE_TESTS") != "1",
        reason="destroys data; set CONCORDANCE_DESTRUCTIVE_TESTS=1 to run (CI does)",
    ),
]


def _run(owner_url: str, *args: str) -> Any:
    """Invoke the CLI in-process against the configured database."""
    from typer.testing import CliRunner

    from concordance.cli import app

    env = dict(os.environ, DATABASE_URL=owner_url)
    return CliRunner().invoke(app, ["db", "reset", *args], env=env)


def _tables(session: Any) -> list[str]:
    from sqlalchemy import text

    return [
        row[0]
        for row in session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        )
    ]


def _count(session: Any, table: str) -> int:
    from sqlalchemy import text

    return int(session.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one())


def test_reset_empties_the_tables_and_keeps_the_schema(owner_session: Any, owner_url: str) -> None:
    from sqlalchemy import text

    owner_session.execute(
        text(
            "INSERT INTO users (id, email, password_hash, role, is_active) "
            "VALUES (gen_random_uuid(), 'reset-test@example.invalid', 'x', 'analyst', true)"
        )
    )
    owner_session.commit()
    assert _count(owner_session, "users") > 0
    before = _tables(owner_session)
    # End this session's transaction first: the count above holds a lock on
    # `users`, and TRUNCATE would wait on it until the statement timeout.
    owner_session.commit()

    result = _run(owner_url, "--yes")
    assert result.exit_code == 0, result.output

    owner_session.rollback()  # see the truncate, not this session's old snapshot
    assert _count(owner_session, "users") == 0
    # The schema survives, migration history included: a reset is a reseed
    # step, not a reason to remigrate a remote database.
    assert _tables(owner_session) == before
    assert _count(owner_session, "alembic_version") == 1


def test_reset_refuses_without_the_confirmation_word(owner_session: Any, owner_url: str) -> None:
    from sqlalchemy import text

    owner_session.execute(
        text(
            "INSERT INTO users (id, email, password_hash, role, is_active) "
            "VALUES (gen_random_uuid(), 'reset-guard@example.invalid', 'x', 'analyst', true)"
        )
    )
    owner_session.commit()

    from typer.testing import CliRunner

    from concordance.cli import app

    result = CliRunner().invoke(
        app,
        ["db", "reset"],
        input="yes\n",  # anything but RESET
        env=dict(os.environ, DATABASE_URL=owner_url),
    )

    assert result.exit_code == 1
    assert "cancelled" in result.output
    owner_session.rollback()
    assert _count(owner_session, "users") > 0
