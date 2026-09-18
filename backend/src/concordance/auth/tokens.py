"""Access tokens and refresh tokens, which are deliberately different things.

**The access token is a JWT and is never stored.** It is short-lived, signed with
`JWT_SECRET`, and carries the user id, role and expiry. The server verifies the
signature and asks the database nothing, which is what keeps a request cheap.
The cost of that choice is that an access token cannot be revoked before it
expires - hence a short TTL.

**The refresh token is opaque random bytes, and only its SHA-256 lives in the
database.** It is long-lived, so it must be revocable, and revocability means
state. Storing the hash rather than the token means a dump of `refresh_tokens`
lets nobody in: the value the client holds cannot be derived from it.

SHA-256 rather than Argon2 for that hash, on purpose. A refresh token is 256
bits of entropy from `secrets`, not a human-chosen password, so there is nothing
to brute-force and no reason to pay Argon2's cost on every refresh. Argon2
exists to make *guessable* secrets expensive to attack.

**Rotation on every refresh.** The old token is revoked as the new one is
issued, so a stolen refresh token is usable at most once, and the theft becomes
visible: the legitimate client's next refresh fails.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from concordance.config import Settings

ALGORITHM = "HS256"

#: Bytes of entropy in a refresh token. 32 is comfortably beyond guessing and
#: keeps the base64 form a reasonable length for a cookie or header.
REFRESH_TOKEN_BYTES = 32

TokenType = Literal["access", "refresh"]


class TokenError(Exception):
    """The token was missing, malformed, expired, or not what was expected."""


@dataclass(frozen=True, slots=True)
class AccessClaims:
    """What a verified access token asserts."""

    user_id: uuid.UUID
    email: str
    role: str
    expires_at: datetime
    issued_at: datetime

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _secret(settings: Settings) -> str:
    secret = settings.JWT_SECRET
    if not secret:
        raise TokenError("JWT_SECRET is not configured; the API cannot issue or verify tokens")
    return str(secret)


def issue_access_token(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    email: str,
    role: str,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """A signed access token and the moment it expires."""
    issued = now or datetime.now(UTC)
    expires = issued + timedelta(seconds=int(settings.JWT_ACCESS_TTL))
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "type": "access",
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        # A unique id per token, so a future denylist has something to name.
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, _secret(settings), algorithm=ALGORITHM), expires


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    """Verify and unpack an access token, or raise `TokenError`."""
    try:
        payload = jwt.decode(token, _secret(settings), algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("token is not valid") from exc

    if payload.get("type") != "access":
        # A refresh token presented as a bearer credential. Refusing it is the
        # point of putting a type in the payload at all.
        raise TokenError("token is not an access token")
    try:
        return AccessClaims(
            user_id=uuid.UUID(str(payload["sub"])),
            email=str(payload.get("email", "")),
            role=str(payload.get("role", "")),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
        )
    except (KeyError, ValueError) as exc:
        raise TokenError("token payload is malformed") from exc


def new_refresh_token(settings: Settings, *, now: datetime | None = None) -> tuple[str, str, datetime]:
    """A refresh token: the value for the client, its hash, and its expiry."""
    issued = now or datetime.now(UTC)
    raw = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    expires = issued + timedelta(seconds=int(settings.JWT_REFRESH_TTL))
    return raw, hash_refresh_token(raw), expires


def hash_refresh_token(raw: str) -> str:
    """SHA-256 hex. The only form of a refresh token the database ever sees."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "ALGORITHM",
    "REFRESH_TOKEN_BYTES",
    "AccessClaims",
    "TokenError",
    "decode_access_token",
    "hash_refresh_token",
    "issue_access_token",
    "new_refresh_token",
]
