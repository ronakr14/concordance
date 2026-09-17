"""Retrying a dropped connection, and refusing to retry anything else.

The distinction is the whole point of the helper. A connection that died is a
network event and the statement never ran; a statement that failed on its own
merits is an answer, and retrying an answer is how a duplicate row gets written.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from concordance.db.retry import is_disconnect, with_reconnect

pytestmark = pytest.mark.unit


class _Session:
    """Just enough session: the helper only ever rolls one back."""

    def __init__(self) -> None:
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1


def _dropped() -> OperationalError:
    exc = OperationalError("SELECT 1", {}, Exception("server closed the connection"))
    exc.connection_invalidated = True
    return exc


def _constraint_violation() -> IntegrityError:
    return IntegrityError("INSERT ...", {}, Exception("duplicate key value"))


def test_a_dropped_connection_is_retried_after_a_rollback() -> None:
    session = _Session()
    attempts: list[int] = []
    slept: list[float] = []

    def flaky() -> str:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise _dropped()
        return "ok"

    result = with_reconnect(
        session,  # type: ignore[arg-type]
        flaky,
        what="test",
        sleep=slept.append,
    )

    assert result == "ok"
    assert len(attempts) == 3
    assert session.rollbacks == 2, "the transaction is unusable after a failure"
    assert slept == [2.0, 4.0], "backoff grows between attempts"


def test_a_constraint_violation_is_not_retried() -> None:
    session = _Session()
    calls: list[int] = []

    def failing() -> Any:
        calls.append(1)
        raise _constraint_violation()

    with pytest.raises(IntegrityError):
        with_reconnect(session, failing, what="test", sleep=lambda _: None)  # type: ignore[arg-type]

    assert len(calls) == 1
    assert session.rollbacks == 0


def test_the_last_attempt_raises_rather_than_retrying_forever() -> None:
    session = _Session()
    calls: list[int] = []

    def always_dropped() -> Any:
        calls.append(1)
        raise _dropped()

    with pytest.raises(OperationalError):
        with_reconnect(
            session,  # type: ignore[arg-type]
            always_dropped,
            what="test",
            attempts=2,
            sleep=lambda _: None,
        )

    assert len(calls) == 2


def test_a_refused_reconnect_is_still_retryable() -> None:
    """The retry is useless if the connect it depends on counts as fatal.

    A provider whose compute is restarting refuses the next connection for a few
    seconds. There is no pooled connection to carry `connection_invalidated`, so
    this case is recognised by message - and only for `OperationalError`.
    """
    refused = OperationalError(
        "SELECT 1", {}, Exception("connection failed: connection refused")
    )
    assert is_disconnect(refused)

    session = _Session()
    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise refused
        return "ok"

    assert with_reconnect(session, flaky, sleep=lambda _: None) == "ok"  # type: ignore[arg-type]
    assert len(calls) == 2


def test_is_disconnect_distinguishes_the_two() -> None:
    assert is_disconnect(_dropped())
    assert not is_disconnect(_constraint_violation())
    assert not is_disconnect(ValueError("not a database error at all"))
    assert not is_disconnect(
        IntegrityError("INSERT ...", {}, Exception("connection failed"))
    ), "an integrity error is never reconsidered, whatever it says"
