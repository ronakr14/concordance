"""Users and refresh tokens."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from concordance.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from concordance.db.enums import UserRole, check_values


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An analyst or an admin.

    `email` is stored as written and uniqued on its lower-cased form through a
    functional index rather than a `citext` column: `citext` is an extension
    that would have to be installed on every deployment for the schema to
    restore, and case-insensitive uniqueness is the only behaviour needed.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.ANALYST)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        check_values("role", UserRole),
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
    )


class RefreshToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A refresh token, stored as a hash.

    The raw token never touches the database: a dump of this table is useless to
    an attacker, which is the whole point of hashing something the client still
    holds in plaintext.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("uq_refresh_tokens_token_hash", "token_hash", unique=True),
    )


__all__ = ["RefreshToken", "User"]
