"""The audit log. Insert and read; never update, never delete.

The database enforces that for the application role - see the revoke in the
first migration. This repository simply has no method that would try.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
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

    def search(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        actor_user_id: uuid.UUID | None = None,
        action: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Page[AuditLog]:
        """Newest first. An `action` ending in `.` matches a family, e.g. `case.`."""
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        if entity_type:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if entity_id:
            stmt = stmt.where(AuditLog.entity_id == str(entity_id))
        if actor_user_id is not None:
            stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
        if action:
            stmt = stmt.where(
                AuditLog.action.startswith(action, autoescape=True)
                if action.endswith(".")
                else AuditLog.action == action
            )
        if date_from is not None:
            stmt = stmt.where(AuditLog.created_at >= datetime.combine(date_from, time.min, UTC))
        if date_to is not None:
            stmt = stmt.where(
                AuditLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min, UTC)
            )
        return paginate(self.session, stmt, limit, offset)

    def recent(self, *, limit: int | None = None, offset: int = 0) -> Page[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
        return paginate(self.session, stmt, limit, offset)


__all__ = ["AuditRepository"]
