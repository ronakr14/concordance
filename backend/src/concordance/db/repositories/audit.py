"""The audit log. Insert and read; never update, never delete.

The database enforces that for the application role - see the revoke in the
first migration. This repository simply has no method that would try.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from concordance.db.models import AuditLog
from concordance.db.repositories.base import Page, paginate


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        actor_user_id: uuid.UUID | None = None,
        actor_role: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        request_id: str | None = None,
        ip: str | None = None,
    ) -> AuditLog:
        row = AuditLog(
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            before=before,
            after=after,
            request_id=request_id,
            ip=ip,
        )
        self.session.add(row)
        return row

    def for_entity(
        self, entity_type: str, entity_id: str, *, limit: int | None = None, offset: int = 0
    ) -> Page[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == str(entity_id))
            .order_by(AuditLog.created_at.desc())
        )
        return paginate(self.session, stmt, limit, offset)

    def recent(self, *, limit: int | None = None, offset: int = 0) -> Page[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
        return paginate(self.session, stmt, limit, offset)


__all__ = ["AuditRepository"]
