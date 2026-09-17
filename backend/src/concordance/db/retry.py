"""Retrying the one database failure that is worth retrying: a dropped connection.

A hosted Postgres - Neon here, but any provider that suspends or migrates a
compute - will occasionally close a connection mid-statement. The client sees
`server closed the connection unexpectedly`, and a five-thousand-record run that
has been going for four minutes dies on a network event that had nothing to do
with the work.

This retries **only** that class of failure, and only for reads that can safely
be repeated:

- A dropped or invalidated connection, recognised through SQLAlchemy's
  `connection_invalidated` flag.
- A connection that could not be *established* on the retry. A provider whose
  compute is restarting refuses the next connect for a few seconds, and treating
  that as fatal makes the retry useless exactly when it is needed. This one has
  to be recognised by message, because there is no pooled connection to carry a
  flag.
- Never a constraint violation, a deadlock, or a statement that failed on its
  own merits. Those are answers, and retrying an answer you did not like is how
  a duplicate row gets written.

The session is rolled back before each retry, because a failed statement leaves
the transaction unusable and the next attempt would fail on that instead. That
rollback is also why the caller has to be idempotent: anything written in the
same transaction before the failure is gone. Every current caller is a read.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from concordance.logging_setup import get_logger

log = get_logger("db.retry")

#: Five attempts over about half a minute. A hosted compute that is restarting
#: refuses connections for several seconds and then answers normally; one that
#: is genuinely down stays down, and the error should surface rather than be
#: retried into a long silence.
DEFAULT_ATTEMPTS = 5
DEFAULT_BACKOFF = 2.0

#: Phrases that mean the connection, not the statement. Matched only against
#: `OperationalError`, which is the class a connection failure arrives as; an
#: `IntegrityError` is never reconsidered no matter what it says.
_CONNECTION_PHRASES = (
    "connection failed",
    "server closed the connection",
    "connection timeout",
    "connection refused",
    "could not connect",
    "terminating connection",
    "consuming input failed",
    "ssl syscall error",
    "connection reset",
)


def is_disconnect(exc: BaseException) -> bool:
    """Whether this is the connection dying rather than the statement failing."""
    if not isinstance(exc, DBAPIError):
        return False
    if exc.connection_invalidated:
        return True
    if not isinstance(exc, OperationalError):
        return False
    text = f"{exc.orig or ''} {exc}".lower()
    return any(phrase in text for phrase in _CONNECTION_PHRASES)


def with_reconnect[T](
    session: Session,
    operation: Callable[[], T],
    *,
    what: str = "query",
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    sleep: Callable[[float], Any] = time.sleep,
) -> T:
    """Run `operation`, retrying it if the connection drops underneath it."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except DBAPIError as exc:
            if not is_disconnect(exc) or attempt == attempts:
                raise
            last = exc
            session.rollback()
            delay = backoff * attempt
            log.warning(
                "db.reconnect",
                what=what,
                attempt=attempt,
                of=attempts,
                delay=delay,
                error=str(exc.orig or exc)[:200],
            )
            sleep(delay)
    raise RuntimeError(f"unreachable: {what} exhausted retries") from last


__all__ = ["DEFAULT_ATTEMPTS", "DEFAULT_BACKOFF", "is_disconnect", "with_reconnect"]
