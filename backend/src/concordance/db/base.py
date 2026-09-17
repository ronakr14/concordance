"""The declarative base, the naming convention, and the shared mixins.

**Why the naming convention matters more than it looks.** Postgres will invent a
name for any constraint you do not name, and Alembic's autogenerate compares the
name it finds against the name it would have produced. Left to the defaults,
`alembic revision --autogenerate` on an unchanged schema emits a migration that
drops and recreates constraints whose only difference is what they are called -
which is exactly the empty-diff check GATE 5 asks for, failing for a cosmetic
reason. Naming every constraint from one template makes that check meaningful.

**Enums are constrained varchars, not native Postgres enum types.** The
checklist asks for one or the other, consistently. A native `CREATE TYPE` is the
better data model in isolation, but adding a value to one requires `ALTER TYPE`
outside a transaction on older servers, autogenerate does not detect changes to
its members, and every downgrade has to drop the type by hand. A varchar with a
`CHECK` constraint is diffable, transactional, and reversible, and the
application-side `StrEnum` in `db.enums` is still the single definition of the
allowed values. The cost is that the database will accept a bad value if someone
writes SQL that bypasses the check - it cannot, the check is on the column.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: One template per constraint kind. `ix` covers indexes, `uq` unique
#: constraints, `ck` checks, `fk` foreign keys, `pk` primary keys.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Every table in the system hangs off this."""

    metadata = metadata

    def as_dict(self) -> dict[str, Any]:
        """Column values as a plain dict - what the audit log records."""
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class UUIDPrimaryKeyMixin:
    """A surrogate key generated in Python, not by the database.

    `uuid4()` client-side rather than `gen_random_uuid()` server-side because
    the loader needs the id before the insert: a `match_result` and its
    `match_candidates` are built in memory and COPY'd together, and waiting for
    the server to assign each parent id would turn one bulk copy into thousands
    of round trips. The server default is still declared so hand-written SQL
    gets a key too.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )


class TimestampMixin:
    """`created_at` / `updated_at`, both server-side.

    Server-side `now()` rather than a Python default so that two rows written in
    the same transaction agree, and so a row inserted by `COPY` or by psql is
    stamped identically to one inserted by the ORM.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CreatedAtMixin:
    """`created_at` alone, for tables that are append-only by design."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "CreatedAtMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "metadata",
]
