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


def database_url(settings: Settings | None = None, *, owner: bool = False) -> str:
    """The connection string for the app role, or for the owner when `owner=True`.

    The API, the worker and every routine command connect as the app role
    (`APP_DATABASE_URL`). A table's owner can never be revoked from its own
    table, so connecting as the owner would make `audit_logs` writable no matter
    what the migration revoked. Only migrations and the two commands that
    `TRUNCATE` connect as the owner. There is deliberately no fallback from one
    to the other: silently falling back to the owner is the failure this split
    exists to prevent.
    """
    resolved = settings or get_settings()
    name = "DATABASE_URL" if owner else "APP_DATABASE_URL"
    url = getattr(resolved, name)
    if not url:
        raise RuntimeError(f"{name} is unset. See .env.example and docs/CHECKLIST.md Stage -1.")
    return str(url)


def _engine_options(settings: Settings) -> dict[str, Any]:
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "future": True,
        "echo": False,
        # Small INSERT batches on purpose. SQLAlchemy would otherwise send
        # one enormous multi-row statement, and a single statement whose
        # payload is megabytes is the shape that stalls over a slow or
        # inspected TLS link - the server finishes, the client keeps
        # waiting, and the run hangs with no error. Smaller statements cost
        # a few more round trips and never do that.
        "insertmanyvalues_page_size": int(settings.DB_INSERT_PAGE_SIZE),
        # `connect_timeout`: without it, a host that accepts the packet but
        # never answers - a stopped service, a firewalled port - leaves the
        # client waiting on the OS default, which on Windows is minutes.
        # `db ping` exists to answer quickly, including when the answer is
        # no.
        #
        # The keepalives prevent a failure that is otherwise silent: a
        # hosted provider that drops a connection mid-query leaves the
        # client blocked on a socket nobody will ever answer, and the run
        # looks hung rather than failed. With these the OS notices in about
        # a minute and psycopg raises.
        "connect_args": {
            "connect_timeout": int(settings.DB_CONNECT_TIMEOUT),
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
            # A statement ceiling as well, because keepalives only notice a
            # socket that has gone quiet, not a server that accepted the
            # query and stopped answering.
            "options": f"-c statement_timeout={int(settings.DB_STATEMENT_TIMEOUT) * 1000}",
        },
    }
    return options


def get_engine(settings: Settings | None = None, **kwargs: Any) -> Engine:
    """The process-wide engine, connected as the app role and created on first use."""
    global _engine
    if _engine is None:
        resolved = settings or get_settings()
        options = _engine_options(resolved)
        options.update(kwargs)
        _engine = create_engine(database_url(resolved), **options)
        log.info("db.engine.created", url=_redact(database_url(resolved)))
    return _engine


def owner_engine(settings: Settings | None = None) -> Engine:
    """A fresh engine connected as the owning role, for `db load` and `db reset` only.

    Not cached, so it never becomes the process-wide engine by accident. The
    caller disposes it.
    """
    resolved = settings or get_settings()
    url = database_url(resolved, owner=True)
    log.info("db.engine.created", url=_redact(url), role="owner")
    return create_engine(url, **_engine_options(resolved))


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
    "owner_engine",
    "ping",
    "session_scope",
]
