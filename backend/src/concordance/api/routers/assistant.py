"""`/assistant` - ask a question in English, get rows and the SQL that made them.

Any signed-in user may ask: the assistant reads five views that hold what the
rest of the UI already shows them. What it cannot do is decided in two places
that do not depend on each other - `assistant.guard` parses the SQL, and the
`concordance_assistant` role the query runs as has `SELECT` on those views and
nothing else.

A refused question is a `200` with `rejected` set, not an error: the refusal is
part of the answer, and the page shows it beside the SQL that caused it. Only a
question that never reached a model - empty, too long - is a `422`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from concordance.api import schemas
from concordance.api.deps import ActorDep, CurrentUser, SessionDep, SettingsDep
from concordance.assistant import service
from concordance.assistant.prompt import QuestionRejectedError
from concordance.errors import InvalidError

router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.get("/schema", response_model=schemas.AssistantSchemaOut)
def schema(_user: CurrentUser) -> schemas.AssistantSchemaOut:
    """The views the assistant may read, as the page lists them for the analyst."""
    return schemas.AssistantSchemaOut(
        views=[schemas.AssistantViewOut.model_validate(v) for v in service.schema()],
        **service.limits(),
    )


@router.post(
    "/query",
    response_model=schemas.AssistantAnswerOut,
    responses={
        409: {"description": "No language model is configured."},
        422: {"description": "The question was empty or too long."},
    },
)
def query(
    body: schemas.AssistantQueryIn,
    session: SessionDep,
    settings: SettingsDep,
    actor: ActorDep,
) -> schemas.AssistantAnswerOut:
    """Turn a question into one bounded, read-only SELECT, run it, and answer.

    Every attempt is written to the audit log - the question, the SQL, the row
    count and the refusal - before this returns.
    """
    try:
        answer = service.ask(session, settings, actor, body.question)
    except QuestionRejectedError as exc:
        raise InvalidError(str(exc), code="bad_question", details={"question": str(exc)}) from exc
    session.commit()
    return schemas.AssistantAnswerOut.model_validate(answer, from_attributes=True)


@router.get("/history", response_model=list[schemas.AssistantHistoryOut])
def history(
    session: SessionDep,
    actor: ActorDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[schemas.AssistantHistoryOut]:
    """This user's recent questions, newest first, read back from the audit log."""
    return [
        schemas.AssistantHistoryOut.model_validate(row)
        for row in service.history(session, actor, limit)
    ]


__all__ = ["router"]
