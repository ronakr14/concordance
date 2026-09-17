"""The engine, the session factory, and the two ways to get a session.

**One engine per process, created lazily.** Importing this module must not open
a connection: the CLI imports half the package to print `--help`, and the
evaluation path never touches Postgres at all. `LLM_ENABLED=false` runs the
whole pipeline with no LLM; the same courtesy applies to the database.

**Two entry points, deliberately.** `session_scope()` is the worker's and the
CLI's: it commits on success, rolls back on any exception, and always closes.
`get_session()` is the FastAPI dependency at Stage 7: it yields and closes but
does not commit, because a request handler decides for itself whether its work
was a success. Sharing one function between the two would mean a failed request
committing whatever the handler had written before it raised.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from concordance.config import Settings, get_settings
from concordance.logging_setup import get_logger

log = get_logger("db.session")

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def database_url(settings: Settings | None = None) -> str:
    url = (settings or get_settings()).DATABASE_URL
    if not url:
        raise RuntimeError(
            "DATABASE_URL is unset - Stage 5 needs Postgres. See docs/CHECKLIST.md Stage -1."
        )
    return str(url)


def get_engine(settings: Settings | None = None, **kwargs: Any) -> Engine:
    """The process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        resolved = settings or get_settings()
        options: dict[str, Any] = {
            "pool_pre_ping": True,
            "pool_size": 5,
            "max_overflow": 5,
            "future": True,
            "echo": False,
            # Without this, a host that accepts the packet but never answers -
            # a stopped service, a firewalled port - leaves the client waiting
            # on the OS default, which on Windows is minutes. `db ping` exists
            # to answer quickly, including when the answer is no.
            "connect_args": {"connect_timeout": int(resolved.DB_CONNECT_TIMEOUT)},
        }
        options.update(kwargs)
        _engine = create_engine(database_url(resolved), **options)
        log.info("db.engine.created", url=_redact(database_url(resolved)))
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> sessionmaker[Session]:
    global _factory
    if _factory is None:
        _factory = sessionmaker(
            bind=get_engine(settings), autoflush=False, expire_on_commit=False, future=True
        )
    return _factory


def dispose_engine() -> None:
    """Drop the engine and the factory. Tests and `db reset` need this."""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """A transactional session: commit on success, roll back on anything else."""
    session = get_sessionmaker(settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Yields a session and closes it; never commits."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def ping(settings: Settings | None = None) -> bool:
    """Whether the configured database answers. Used by `concordance db ping`."""
    try:
        with get_engine(settings).connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        log.warning("db.ping.failed", error=str(exc))
        return False
    return True


def _redact(url: str) -> str:
    """`postgresql://user:secret@host/db` -> `postgresql://user:***@host/db`."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


__all__ = [
    "database_url",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "ping",
    "session_scope",
]
