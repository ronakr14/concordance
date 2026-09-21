"""`audit_logs` is append-only, and the database is what enforces it.

A convention that the application never updates an audit row is worth little:
the defence a regulator cares about is one that holds when the application is
wrong. So the migration revokes `UPDATE` and `DELETE` on `audit_logs` from the
role the application connects as, and these tests prove the revoke bites rather
than assuming a migration that ran did what it said.

The distinction the tests turn on: a table's owner can never be revoked from
its own table. The owning role runs migrations; `concordance_app` is what the
application uses. If those two are ever the same role, every assertion below
passes for the wrong reason, so `test_the_app_role_is_not_the_owner` checks
that first.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.integration


def _insert_row(session) -> int:
    row_id = session.execute(
        text(
            """
            INSERT INTO audit_logs (actor_role, action, entity_type, entity_id, before, after)
            VALUES ('analyst', :action, 'test', :entity, '{}'::jsonb, '{}'::jsonb)
            RETURNING id
            """
        ),
        {"action": "immutability-probe", "entity": str(uuid.uuid4())},
    ).scalar_one()
    session.commit()
    return int(row_id)


def test_the_app_role_is_not_the_owner(app_session, owner_session) -> None:
    app_role = app_session.execute(text("SELECT current_user")).scalar_one()
    owner = owner_session.execute(
        text("SELECT tableowner FROM pg_tables WHERE tablename = 'audit_logs'")
    ).scalar_one()
    assert app_role != owner, (
        "the application connects as the table owner, so the revoke below cannot "
        "be tested - an owner keeps every privilege on its own table"
    )


def test_the_app_role_can_append(app_session) -> None:
    """Append-only means append: the revoke must not have taken INSERT with it."""
    row_id = _insert_row(app_session)
    assert row_id > 0


def test_update_is_rejected_for_the_app_role(app_session) -> None:
    row_id = _insert_row(app_session)
    with pytest.raises(ProgrammingError) as caught:
        app_session.execute(
            text("UPDATE audit_logs SET action = 'tampered' WHERE id = :id"), {"id": row_id}
        )
    assert "permission denied" in str(caught.value).lower()
    app_session.rollback()


def test_delete_is_rejected_for_the_app_role(app_session) -> None:
    row_id = _insert_row(app_session)
    with pytest.raises(ProgrammingError) as caught:
        app_session.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": row_id})
    assert "permission denied" in str(caught.value).lower()
    app_session.rollback()


def test_the_row_survives_the_attempts(app_session) -> None:
    """The point of the revoke: the evidence is still there afterwards."""
    row_id = _insert_row(app_session)
    for statement in (
        "UPDATE audit_logs SET action = 'tampered' WHERE id = :id",
        "DELETE FROM audit_logs WHERE id = :id",
    ):
        with pytest.raises(ProgrammingError):
            app_session.execute(text(statement), {"id": row_id})
        app_session.rollback()

    action = app_session.execute(
        text("SELECT action FROM audit_logs WHERE id = :id"), {"id": row_id}
    ).scalar_one()
    assert action == "immutability-probe"


def test_the_engine_the_application_uses_cannot_rewrite_the_log(app_url: str, owner_url: str) -> None:
    """The tests above prove `concordance_app` cannot update the log. This one
    proves it is `concordance_app` the API and worker connect as - the revoke is
    worth nothing if the running system uses the owner instead."""
    from concordance.config import Settings
    from concordance.db.session import dispose_engine, get_engine

    dispose_engine()
    settings = Settings(_env_file=None, DATABASE_URL=owner_url, APP_DATABASE_URL=app_url)
    try:
        with get_engine(settings).connect() as connection:
            owner = connection.execute(
                text("SELECT tableowner FROM pg_tables WHERE tablename = 'audit_logs'")
            ).scalar_one()
            assert connection.execute(text("SELECT current_user")).scalar_one() != owner
            with pytest.raises(ProgrammingError, match="permission denied"):
                connection.execute(text("DELETE FROM audit_logs WHERE id = -1"))
            connection.rollback()
    finally:
        dispose_engine()
