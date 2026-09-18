"""Passwords and tokens: the parts of auth that need no database.

The assertions here are about the properties that make the scheme worth having
rather than about the library doing its job - that a refresh token cannot be
presented as a bearer credential, that a tampered signature is refused, that an
expired token is refused even though it is otherwise valid, and that the
database never sees a refresh token in the form the client holds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from concordance.auth.passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPasswordError,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)
from concordance.auth.tokens import (
    ALGORITHM,
    TokenError,
    decode_access_token,
    hash_refresh_token,
    issue_access_token,
    new_refresh_token,
)
from concordance.config import Settings

pytestmark = pytest.mark.unit

GOOD_PASSWORD = "a reasonable passphrase 42"


@pytest.fixture
def settings() -> Settings:
    return Settings(JWT_SECRET="test-signing-key-long-enough-to-be-plausible", JWT_ACCESS_TTL=900)


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------


def test_a_hash_verifies_its_own_password_and_nothing_else() -> None:
    stored = hash_password(GOOD_PASSWORD)
    assert stored != GOOD_PASSWORD, "the password itself is never stored"
    assert stored.startswith("$argon2id$"), "argon2id, not argon2i or argon2d"
    assert verify_password(stored, GOOD_PASSWORD)
    assert not verify_password(stored, GOOD_PASSWORD + " ")
    assert not verify_password(stored, "")


def test_two_hashes_of_one_password_differ() -> None:
    """Per-password salt. Identical hashes would make the table a rainbow table."""
    assert hash_password(GOOD_PASSWORD) != hash_password(GOOD_PASSWORD)


def test_a_garbage_hash_is_a_failed_login_not_a_crash() -> None:
    assert not verify_password("not a hash at all", GOOD_PASSWORD)
    assert needs_rehash("not a hash at all"), "an unreadable hash should be replaced"


def test_a_current_hash_does_not_need_rehashing() -> None:
    assert not needs_rehash(hash_password(GOOD_PASSWORD))


@pytest.mark.parametrize(
    "password",
    ["short", "password123!", "aaaaaaaaaaaaaaaa", "12345678", "Password1"],
)
def test_weak_passwords_are_refused_before_hashing(password: str) -> None:
    with pytest.raises(WeakPasswordError):
        validate_password(password)


def test_the_length_floor_is_where_it_says_it_is() -> None:
    validate_password("x" * (MIN_PASSWORD_LENGTH - 6) + "abcdef")
    with pytest.raises(WeakPasswordError, match="at least"):
        validate_password("abcdefghijk")  # one short


def test_an_enormous_password_is_refused_rather_than_hashed() -> None:
    """A megabyte password is a denial of service on the hasher, not a careful user."""
    with pytest.raises(WeakPasswordError, match="at most"):
        validate_password("x" * 2000)


# --------------------------------------------------------------------------
# access tokens
# --------------------------------------------------------------------------


def test_an_access_token_round_trips_its_claims(settings: Settings) -> None:
    user_id = uuid.uuid4()
    token, expires = issue_access_token(
        settings, user_id=user_id, email="a@example.com", role="admin"
    )

    claims = decode_access_token(settings, token)
    assert claims.user_id == user_id
    assert claims.email == "a@example.com"
    assert claims.role == "admin"
    assert claims.is_admin
    assert abs((claims.expires_at - expires).total_seconds()) < 1
    assert 890 < (expires - datetime.now(UTC)).total_seconds() <= 900


def test_a_tampered_token_is_refused(settings: Settings) -> None:
    token, _ = issue_access_token(
        settings, user_id=uuid.uuid4(), email="a@example.com", role="analyst"
    )
    head, payload, signature = token.split(".")
    forged = f"{head}.{payload}.{signature[:-4]}AAAA"

    with pytest.raises(TokenError):
        decode_access_token(settings, forged)


def test_a_token_signed_with_another_secret_is_refused(settings: Settings) -> None:
    other = Settings(JWT_SECRET="a completely different signing secret")
    token, _ = issue_access_token(
        other, user_id=uuid.uuid4(), email="a@example.com", role="analyst"
    )

    with pytest.raises(TokenError, match="not valid"):
        decode_access_token(settings, token)


def test_an_expired_token_is_refused(settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(hours=2)
    token, _ = issue_access_token(
        settings, user_id=uuid.uuid4(), email="a@example.com", role="analyst", now=past
    )

    with pytest.raises(TokenError, match="expired"):
        decode_access_token(settings, token)


def test_a_refresh_style_token_cannot_be_used_as_a_bearer_credential(settings: Settings) -> None:
    """The `type` claim exists for exactly this: a long-lived credential must not
    be accepted where a short-lived one is expected."""
    payload = {
        "sub": str(uuid.uuid4()),
        "email": "a@example.com",
        "role": "admin",
        "type": "refresh",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(days=7)).timestamp()),
    }
    forged = jwt.encode(payload, str(settings.JWT_SECRET), algorithm=ALGORITHM)

    with pytest.raises(TokenError, match="not an access token"):
        decode_access_token(settings, forged)


def test_no_secret_configured_is_an_error_not_an_unsigned_token() -> None:
    blank = Settings(JWT_SECRET=None)
    with pytest.raises(TokenError, match="JWT_SECRET"):
        issue_access_token(blank, user_id=uuid.uuid4(), email="a@example.com", role="analyst")


# --------------------------------------------------------------------------
# refresh tokens
# --------------------------------------------------------------------------


def test_a_refresh_token_is_opaque_and_only_its_hash_is_storable(settings: Settings) -> None:
    raw, stored_hash, expires = new_refresh_token(settings)

    assert "." not in raw, "opaque random bytes, not a JWT"
    assert len(raw) >= 40
    assert stored_hash == hash_refresh_token(raw)
    assert len(stored_hash) == 64 and int(stored_hash, 16) >= 0, "sha-256 hex"
    assert raw not in stored_hash
    assert expires > datetime.now(UTC) + timedelta(days=13)


def test_two_refresh_tokens_are_never_the_same(settings: Settings) -> None:
    first, _, _ = new_refresh_token(settings)
    second, _, _ = new_refresh_token(settings)
    assert first != second
