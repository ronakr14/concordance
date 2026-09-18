"""Registration, login, refresh, logout - the rules, with no HTTP in them.

Kept separate from the router so the security decisions are testable without a
client, and so the same rules could be driven from the CLI. Three of those
decisions are worth stating plainly, because each is a place where the obvious
implementation is wrong:

**A failed login says the same thing every time.** Wrong password, unknown
email, deactivated account - all `InvalidCredentialsError`, all identical to the
caller. An error that distinguishes them turns the login form into an oracle for
which addresses have accounts.

**A failed login costs the same time as a successful one.** When no user is
found, a dummy verification runs anyway, so the response time does not reveal
whether the address exists. Without it, Argon2's own cost becomes the side
channel: a hit takes 150 ms and a miss returns instantly.

**Refresh rotates.** The presented token is revoked as the new pair is issued.
If a token is presented twice - which means it leaked - the second attempt
fails, and that failure is the only signal anyone gets that the theft happened,
so it is logged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from concordance.auth.passwords import hash_password, needs_rehash, verify_password
from concordance.auth.tokens import (
    hash_refresh_token,
    issue_access_token,
    new_refresh_token,
)
from concordance.config import Settings
from concordance.db.enums import UserRole
from concordance.db.models import User
from concordance.db.repositories.audit import AuditRepository
from concordance.db.repositories.users import UserRepository
from concordance.logging_setup import get_logger

log = get_logger("auth.service")

#: A real Argon2 hash of a value nobody knows, verified against whenever no user
#: is found. Its only job is to consume the same time a real verification would.
_DUMMY_HASH = hash_password("dummy password for constant time comparison")


class AuthError(Exception):
    """Base for everything this module refuses to do."""


class InvalidCredentialsError(AuthError):
    """Wrong password, unknown email, or an inactive account - indistinguishable."""


class EmailTakenError(AuthError):
    """That address already has an account."""


class InvalidRefreshTokenError(AuthError):
    """The refresh token is unknown, expired, or already used."""


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_at: datetime
    user: User

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": "bearer",
            "expires_at": self.expires_at.isoformat(),
        }


def register(
    session: Session,
    settings: Settings,  # noqa: ARG001 - every entry point here takes the same two
    *,
    email: str,
    password: str,
    full_name: str | None = None,
    role: UserRole | str = UserRole.ANALYST,
    actor_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> User:
    """Create a user. Raises `WeakPasswordError` or `EmailTakenError`."""
    users = UserRepository(session)
    if users.by_email(email) is not None:
        raise EmailTakenError("an account with that email already exists")

    user = users.create(
        email=email.strip(),
        password_hash=hash_password(password),
        full_name=full_name,
        role=role,
    )
    session.flush()
    AuditRepository(session).record(
        action="user.registered",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=actor_id,
        actor_role="system" if actor_id is None else None,
        after={"email": user.email, "role": user.role},
        request_id=request_id,
    )
    log.info("auth.registered", user_id=str(user.id), role=user.role)
    return user


def login(
    session: Session,
    settings: Settings,
    *,
    email: str,
    password: str,
    request_id: str | None = None,
    ip: str | None = None,
) -> TokenPair:
    """Verify credentials and issue a token pair."""
    users = UserRepository(session)
    user = users.by_email(email)

    if user is None:
        # Spend the same time as a real verification before refusing.
        verify_password(_DUMMY_HASH, password)
        log.info("auth.login.failed", reason="no such user")
        raise InvalidCredentialsError("email or password is incorrect")

    if not verify_password(user.password_hash, password):
        log.info("auth.login.failed", reason="bad password", user_id=str(user.id))
        raise InvalidCredentialsError("email or password is incorrect")

    if not user.is_active:
        log.info("auth.login.failed", reason="inactive", user_id=str(user.id))
        raise InvalidCredentialsError("email or password is incorrect")

    if needs_rehash(user.password_hash):
        # The parameters have been raised since this hash was written. The
        # password is in hand exactly once - now - so upgrade it here or not
        # at all.
        user.password_hash = hash_password(password)
        log.info("auth.password.rehashed", user_id=str(user.id))

    pair = _issue(session, settings, user)
    AuditRepository(session).record(
        action="user.login",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=user.id,
        actor_role=user.role,
        request_id=request_id,
        ip=ip,
    )
    log.info("auth.login", user_id=str(user.id), role=user.role)
    return pair


def refresh(
    session: Session,
    settings: Settings,
    *,
    refresh_token: str,
    request_id: str | None = None,
) -> TokenPair:
    """Exchange a refresh token for a new pair, revoking the one presented."""
    users = UserRepository(session)
    token_hash = hash_refresh_token(refresh_token)
    stored = users.find_refresh_token(token_hash)

    if stored is None:
        raise InvalidRefreshTokenError("refresh token is not recognised")
    if stored.revoked_at is not None:
        # Presented twice. Either the client is confused or the token leaked,
        # and the safe reading is the second one: cut every session for this
        # user rather than issue a pair to whoever asked.
        log.warning("auth.refresh.reuse", user_id=str(stored.user_id))
        users.revoke_all_for_user(stored.user_id)
        raise InvalidRefreshTokenError("refresh token has already been used")
    if stored.expires_at <= datetime.now(UTC):
        raise InvalidRefreshTokenError("refresh token has expired")

    user = users.get(stored.user_id)
    if user is None or not user.is_active:
        raise InvalidRefreshTokenError("refresh token is not recognised")

    users.revoke_refresh_token(token_hash)
    pair = _issue(session, settings, user)
    AuditRepository(session).record(
        action="user.refresh",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=user.id,
        actor_role=user.role,
        request_id=request_id,
    )
    return pair


def logout(
    session: Session,
    *,
    refresh_token: str,
    user: User | None = None,
    request_id: str | None = None,
) -> None:
    """Revoke one refresh token. Unknown tokens are not an error."""
    users = UserRepository(session)
    users.revoke_refresh_token(hash_refresh_token(refresh_token))
    if user is not None:
        AuditRepository(session).record(
            action="user.logout",
            entity_type="user",
            entity_id=str(user.id),
            actor_user_id=user.id,
            actor_role=user.role,
            request_id=request_id,
        )


def _issue(session: Session, settings: Settings, user: User) -> TokenPair:
    access, expires = issue_access_token(
        settings, user_id=user.id, email=user.email, role=user.role
    )
    raw, token_hash, refresh_expires = new_refresh_token(settings)
    UserRepository(session).add_refresh_token(
        user_id=user.id, token_hash=token_hash, expires_at=refresh_expires
    )
    return TokenPair(access_token=access, refresh_token=raw, expires_at=expires, user=user)


__all__ = [
    "AuthError",
    "EmailTakenError",
    "InvalidCredentialsError",
    "InvalidRefreshTokenError",
    "TokenPair",
    "login",
    "logout",
    "refresh",
    "register",
]
