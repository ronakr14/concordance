"""Question in, rows out - with every step recorded and none of them trusted.

The path is deliberately short and each stage can refuse:

1. `prompt.clean` caps the question and takes the delimiter out of it.
2. The model writes SQL (`llm.router`, so the same chain, cache and failover as
   adjudication).
3. `guard.check` parses it, refuses anything that is not a bounded read of the
   whitelisted views, and **regenerates the SQL** that will actually run.
4. It executes in a transaction that is `READ ONLY`, has assumed the
   `concordance_assistant` role, has a statement timeout, and is rolled back.
5. The attempt is written to the audit log either way - the question, the SQL,
   the row count, and the refusal when there was one.

The answer always carries the SQL, refused or not. An analyst who cannot see
the query cannot tell a right answer from a confident one.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from concordance.assistant.guard import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SqlRejectedError,
    check,
)
from concordance.assistant.prompt import (
    PROMPT_VERSION,
    QuestionRejectedError,
    clean,
    extract_sql,
    render,
)
from concordance.assistant.views import READONLY_ROLE, VIEWS
from concordance.audit import service as audit
from concordance.audit.service import Actor
from concordance.config import Settings
from concordance.errors import ConflictError
from concordance.llm.errors import LLMError
from concordance.llm.router import LLMRouter
from concordance.logging_setup import get_logger

log = get_logger("assistant.service")

#: Values wider than this are truncated for display; a page is not a file export.
MAX_CELL_CHARS = 500


@dataclass
class Answer:
    """What the page renders: the question, the SQL, and the rows or the refusal."""

    question: str
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    rejected: str | None = None
    rejection_code: str | None = None
    prompt_version: str = PROMPT_VERSION
    model: str | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.rejected is None


def ask(
    session: Session,
    settings: Settings,
    actor: Actor,
    question: str,
    *,
    router: LLMRouter | None = None,
) -> Answer:
    """Answer one question. Refusals are returned, not raised - except bad input."""
    started = time.perf_counter()
    asked = clean(question)  # raises QuestionRejectedError, which the API maps to 422
    answer = Answer(question=asked)

    chain = router if router is not None else LLMRouter.from_settings(settings)
    if not chain.available:
        raise ConflictError(
            "the assistant needs a language model; LLM_ENABLED is false or no provider "
            "has an API key",
            code="llm_disabled",
        )

    try:
        # Plain text, not JSON mode: the answer is one SQL statement, and the
        # guard is what validates it. Asking for JSON would only add a wrapper
        # to unwrap.
        response = chain.complete(render(asked), json_mode=False)
        answer.model = f"{response.provider}/{response.model}"
        written = extract_sql(response.content)
    except LLMError as exc:
        answer.rejected = f"no model answered: {exc}"
        answer.rejection_code = "llm_unavailable"
        _record(session, actor, answer)
        return answer

    try:
        safe = check(
            written, default_limit=min(DEFAULT_LIMIT, settings.ASSISTANT_MAX_ROWS),
            max_limit=settings.ASSISTANT_MAX_ROWS,
        )
    except SqlRejectedError as exc:
        # The rejected SQL is shown: the analyst should see what was refused,
        # and so should anyone reading the audit log afterwards.
        answer.sql = written[:MAX_CELL_CHARS]
        answer.rejected = exc.reason
        answer.rejection_code = exc.code
        answer.seconds = round(time.perf_counter() - started, 3)
        _record(session, actor, answer)
        log.warning("assistant.rejected", code=exc.code, reason=exc.reason, sql=answer.sql)
        return answer

    answer.sql = safe.sql
    answer.notes = list(safe.notes)
    try:
        answer.columns, answer.rows = _run(session, settings, safe.sql)
    except SQLAlchemyError as exc:
        session.rollback()
        answer.rejected = _readable(exc)
        answer.rejection_code = "execution_failed"
        answer.seconds = round(time.perf_counter() - started, 3)
        _record(session, actor, answer)
        return answer

    answer.row_count = len(answer.rows)
    answer.truncated = answer.row_count >= safe.limit
    if answer.truncated:
        answer.notes.append(f"showing the first {safe.limit} rows")
    answer.seconds = round(time.perf_counter() - started, 3)
    _record(session, actor, answer)
    log.info(
        "assistant.answered",
        rows=answer.row_count,
        seconds=answer.seconds,
        tables=sorted(safe.tables),
    )
    return answer


def _run(session: Session, settings: Settings, sql: str) -> tuple[list[str], list[list[Any]]]:
    """Execute as the read-only role, under a timeout, and roll back.

    `SET LOCAL ROLE` rather than a second connection: the privileges are the
    role's for the length of this transaction, and there is no second password
    to store. The rollback is what makes the timeout and the role local -
    nothing the query did outlives it, and nothing it set survives.
    """
    session.rollback()  # start clean: this transaction is about to change role
    try:
        session.execute(text(f"SET LOCAL ROLE {READONLY_ROLE}"))
        session.execute(text("SET TRANSACTION READ ONLY"))
        # `SET` takes no bind parameters, so the value is an int by construction
        # (pydantic validated the setting) rather than by escaping.
        session.execute(text(f"SET LOCAL statement_timeout = {int(settings.ASSISTANT_TIMEOUT_MS)}"))
        result = session.execute(text(sql))
        columns = list(result.keys())
        rows = [[_cell(value) for value in row] for row in result.fetchall()]
        return columns, rows
    finally:
        # Always: the app's own session must not keep the assistant's role.
        session.rollback()


def _cell(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…"
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)


def _readable(exc: SQLAlchemyError) -> str:
    """The database's complaint, without the driver's stack of wrappers."""
    message = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
    if "statement timeout" in message.lower() or "canceling statement" in message.lower():
        return "the query took too long and was cancelled; ask for something narrower"
    if "permission denied" in message.lower():
        return "the read-only role may not read that; the assistant sees only its own views"
    return f"the query failed: {message[:300]}"


def _record(session: Session, actor: Actor, answer: Answer) -> None:
    """Every attempt, answered or refused, in the audit log."""
    audit.record(
        session,
        actor,
        "assistant.query",
        entity_type="assistant",
        entity_id=uuid.uuid4(),
        after={
            "question": answer.question,
            "sql": answer.sql,
            "rows": answer.row_count,
            "rejected": answer.rejected,
            "code": answer.rejection_code,
            "model": answer.model,
            "prompt_version": answer.prompt_version,
            "seconds": answer.seconds,
        },
    )


def history(session: Session, actor: Actor, limit: int = 20) -> list[dict[str, Any]]:
    """This user's recent questions, newest first, read back from the audit log."""
    from concordance.db.models import AuditLog

    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "assistant.query", AuditLog.actor_user_id == actor.user_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "asked_at": row.created_at,
            "question": (row.after or {}).get("question", ""),
            "sql": (row.after or {}).get("sql"),
            "rows": (row.after or {}).get("rows", 0),
            "rejected": (row.after or {}).get("rejected"),
        }
        for row in rows
    ]


def schema() -> list[dict[str, Any]]:
    """The views the assistant may read, as the page lists them."""
    return [
        {
            "name": view.name,
            "about": view.about,
            "columns": [{"name": name, "about": about} for name, about in view.columns],
        }
        for view in VIEWS
    ]


def limits() -> dict[str, int]:
    return {"default_limit": DEFAULT_LIMIT, "max_limit": MAX_LIMIT}


__all__ = ["Answer", "QuestionRejectedError", "ask", "history", "limits", "schema"]
