"""Database access for integration tests.

The root `conftest` deliberately hides `.env` from the suite, so that a test's
result never depends on how the developer's machine happens to be configured.
That is right for everything that can run without a database, and it is exactly
wrong for the handful of tests whose whole subject is the database.

These fixtures therefore read `.env` directly rather than through `Settings`,
and skip when it names no database. Reading the file rather than the
environment keeps the isolation the root conftest provides: nothing here leaks
a setting back into the process for other tests to pick up.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_file_value(name: str) -> str | None:
    env = _REPO_ROOT / ".env"
    if not env.is_file():
        return None
    match = re.search(rf"^{name}=(.+)$", env.read_text(encoding="utf-8"), re.M)
    return match.group(1).strip() if match else None


@pytest.fixture(scope="session")
def owner_url() -> str:
    url = _env_file_value("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not configured in .env")
    return url


@pytest.fixture(scope="session")
def app_url() -> str:
    url = _env_file_value("APP_DATABASE_URL")
    if not url:
        pytest.skip("APP_DATABASE_URL is not configured in .env")
    return url


@pytest.fixture
def owner_session(owner_url: str) -> Iterator[Any]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(owner_url, connect_args={"connect_timeout": 20})
    factory = sessionmaker(bind=engine, future=True)
    with factory() as session:
        yield session
    engine.dispose()


@pytest.fixture
def app_session(app_url: str) -> Iterator[Any]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(app_url, connect_args={"connect_timeout": 20})
    factory = sessionmaker(bind=engine, future=True)
    with factory() as session:
        yield session
    engine.dispose()
