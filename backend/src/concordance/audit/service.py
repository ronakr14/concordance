"""Who is acting, and the one function every mutation records itself through.

**A service, not a middleware.** A middleware sees a request and a status code;
it does not see which case was closed, what its status was before, or which
provider an approval chose. Those are the parts of an audit row anyone reads,
so the row is written by the code that knows them - inside the same
transaction as the change it describes. A failed audit write therefore fails
the action, and an action that committed always has its row: there is no
window in which one exists without the other.

**`Actor` travels with the call.** It carries the user, their role at the time,
the request's correlation id and the client address, so a service does not have
to be handed four loose arguments to write one row - and cannot forget one.
The scheduler and the worker act as `Actor.system()`, which records a null user
and the role `system` rather than inventing an account.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from concordance.db.models import AuditLog
from concordance.db.repositories.audit import AuditRepository

SYSTEM_ROLE = "system"


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: uuid.UUID | None
    role: str
    request_id: str | None = None
    ip: str | None = None

    @classmethod
    def system(cls, request_id: str | None = None) -> Actor:
        return cls(user_id=None, role=SYSTEM_ROLE, request_id=request_id)

    @classmethod
    def of(cls, user: Any, *, request_id: str | None = None, ip: str | None = None) -> Actor:
        return cls(user_id=user.id, role=str(user.role), request_id=request_id, ip=ip)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def record(
    session: Session,
    actor: Actor,
    action: str,
    *,
    entity_type: str,
    entity_id: Any,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """Add one audit row to the caller's transaction."""
    return AuditRepository(session).record(
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        before=_jsonable(before),
        after=_jsonable(after),
        request_id=actor.request_id,
        ip=actor.ip,
    )


def _jsonable(value: Any) -> Any:
    """JSONB takes what `json` takes. UUIDs and dates are the usual strays."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


__all__ = ["SYSTEM_ROLE", "Actor", "record"]
