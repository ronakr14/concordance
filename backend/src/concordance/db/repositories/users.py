"""Users and refresh tokens.

No password hashing here - that is Stage 7's `auth` module. A repository that
hashed would make the hash algorithm a storage decision, and rotating it would
mean editing the table layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from concordance.db.enums import UserRole
from concordance.db.models import RefreshToken, User
from concordance.db.repositories.base import Page, paginate


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.session.get(User, user_id)

    def by_email(self, email: str) -> User | None:
        """Case-insensitive, matching the functional unique index on the table."""
        return self.session.scalar(select(User).where(func.lower(User.email) == email.lower()))

    def create(
        self,
        *,
        email: str,
        password_hash: str,
        full_name: str | None = None,
        role: UserRole | str = UserRole.ANALYST,
    ) -> User:
        user = User(email=email, password_hash=password_hash, full_name=full_name, role=str(role))
        self.session.add(user)
        return user

    def list_users(self, *, limit: int | None = None, offset: int = 0) -> Page[User]:
        stmt = select(User).order_by(User.created_at)
        return paginate(self.session, stmt, limit, offset)

    def deactivate(self, user_id: uuid.UUID) -> None:
        """Users are deactivated, never deleted - audit rows point at them."""
        self.session.execute(update(User).where(User.id == user_id).values(is_active=False))

    # -- refresh tokens ---------------------------------------------------
    def add_refresh_token(
        self, *, user_id: uuid.UUID, token_hash: str, expires_at: datetime
    ) -> RefreshToken:
        token = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        self.session.add(token)
        return token

    def find_refresh_token(self, token_hash: str) -> RefreshToken | None:
        return self.session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )

    def revoke_refresh_token(self, token_hash: str) -> None:
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    def revoke_all_for_user(self, user_id: uuid.UUID) -> None:
        """What a password change and a forced logout both need."""
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )


__all__ = ["UserRepository"]
