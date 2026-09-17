"""Alembic environment.

The URL comes from `Settings`, never from `alembic.ini`. One source for the
connection string means `alembic upgrade head` and the application cannot
disagree about which database they are talking to, and it keeps the credential
out of a tracked file.

`compare_type` and `compare_server_default` are both on. Without them,
autogenerate ignores a column whose type or default changed, which turns the
GATE 5 empty-diff check into a check that the table names match.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from concordance.config import get_settings
from concordance.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: Tables an extension owns. Nothing installs one today; the guard is here so
#: a future extension cannot make autogenerate propose dropping its tables.
IGNORED_TABLES = {"spatial_ref_sys"}

target_metadata = Base.metadata

settings = get_settings()
if not settings.DATABASE_URL:
    raise SystemExit(
        "DATABASE_URL is unset. Copy .env.example to .env and point it at Postgres 16."
    )
config.set_main_option("sqlalchemy.url", str(settings.DATABASE_URL))


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ARG001 - alembic's signature
    """Ignore objects this project did not create.

    `pg_trgm` installs no tables, but an operator class or an extension-owned
    index showing up in a diff would be noise, and a future PostGIS-style
    extension would produce a migration that tries to drop it.
    """
    return not (type_ == "table" and name in IGNORED_TABLES)


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it - `alembic upgrade head --sql`."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
