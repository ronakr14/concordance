"""A run survives a connection dropped while one of its chunks is being written.

Found building the demo database: a 5,000-record run against Neon died at the
third chunk when the server closed the connection mid-insert, throwing away the
3,500 decisions still to come. The chunk is now written again after a reconnect
- the same decisions, not re-scored - from the writer state it started with.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from concordance.jobs import reconcile

pytestmark = pytest.mark.unit


def _dropped() -> OperationalError:
    return OperationalError(
        "INSERT INTO match_results ...", {}, Exception("server closed the connection unexpectedly")
    )


class Session:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class Writer:
    run_id = "run-1"

    def __init__(self) -> None:
        self.written: list[tuple[Any, str | None]] = []
        self.state = 0
        self.restored: list[int] = []

    def checkpoint(self) -> int:
        return self.state

    def restore(self, mark: int) -> None:
        self.restored.append(mark)
        self.state = mark

    def write(self, result: Any, call_key: str | None = None) -> None:
        self.written.append((result, call_key))
        self.state += 1

    def flush_chunk(self) -> None: ...


@pytest.fixture(autouse=True)
def _no_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconcile, "_progress", lambda run, engine: None)


DECIDED = [("r1", None), ("r2", "llm-key-2")]


def test_a_dropped_connection_rewrites_the_same_chunk_from_where_it_began() -> None:
    session, writer = Session([_dropped()]), Writer()

    reconcile._persist_chunk(session, writer, None, None, DECIDED, sleep=lambda _s: None)  # type: ignore[arg-type]

    assert session.rollbacks == 1 and session.commits == 1
    assert writer.restored == [0]  # back to the state before the chunk, not after it
    assert writer.written == DECIDED + DECIDED  # the same decisions, written twice
    assert writer.state == len(DECIDED)  # and counted once


def test_a_statement_that_fails_on_its_own_merits_still_fails_the_run() -> None:
    refused = ProgrammingError("INSERT ...", {}, Exception("permission denied"))
    session, writer = Session([refused]), Writer()

    with pytest.raises(ProgrammingError):
        reconcile._persist_chunk(session, writer, None, None, DECIDED, sleep=lambda _s: None)  # type: ignore[arg-type]
    assert writer.restored == []


def test_a_connection_that_keeps_dropping_gives_up() -> None:
    session, writer = Session([_dropped() for _ in range(3)]), Writer()

    with pytest.raises(OperationalError):
        reconcile._persist_chunk(
            session, writer, None, None, DECIDED, attempts=3, sleep=lambda _s: None  # type: ignore[arg-type]
        )
    assert writer.restored == [0, 0]


def test_the_real_writer_restores_everything_a_chunk_moved() -> None:
    import uuid

    writer = reconcile._ResultWriter(object(), uuid.uuid4())  # type: ignore[arg-type]
    writer._current[("OIG", "S1")] = uuid.uuid4()
    writer.tally.update({("individual", (1, 2)): 3})
    mark = writer.checkpoint()

    writer._current[("OIG", "S2")] = uuid.uuid4()
    writer.tally.update({("individual", (1, 2)): 3})
    writer.superseded += 1
    writer.conflicts += 1
    writer._pending_candidates.append(object())  # type: ignore[arg-type]
    writer._pending_links.append(lambda: None)

    writer.restore(mark)
    assert list(writer._current) == [("OIG", "S1")]
    assert writer.tally == {("individual", (1, 2)): 3}
    assert (writer.superseded, writer.conflicts) == (0, 0)
    assert writer._pending_candidates == [] and writer._pending_links == []


def test_a_dns_failure_on_the_reconnect_is_retried_too() -> None:
    """What actually happened on the second demo run: the connection dropped,
    and the reconnect then failed to resolve the host while the link recovered."""
    dns = OperationalError("connect", {}, Exception("failed to resolve host 'ep-x.neon.tech'"))
    session, writer = Session([_dropped(), dns]), Writer()

    reconcile._persist_chunk(session, writer, None, None, DECIDED, sleep=lambda _s: None)  # type: ignore[arg-type]
    assert session.commits == 1 and writer.restored == [0, 0]
