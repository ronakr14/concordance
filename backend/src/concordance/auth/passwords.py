"""Password hashing, and what counts as a password.

**Argon2id**, because it is the algorithm designed for this and the one that
makes a stolen table expensive rather than merely inconvenient. The parameters
below are the argon2-cffi defaults with the memory cost raised: they target
roughly 50-100 ms per verification on a developer machine, which is unnoticeable
to a person logging in and ruinous to someone working through a leaked table.

**`needs_rehash` is checked on every successful login.** Parameters get raised
over time; a user whose hash predates the change is upgraded transparently the
next time they sign in, rather than keeping a weaker hash until they happen to
change their password.

**Strength validation refuses the passwords that actually get used**, not the
ones a regex finds displeasing. Length does more work than character classes, so
the floor is twelve characters, and a small list of the obvious ones is refused
outright. There is deliberately no "must contain a symbol" rule: it produces
`Password1!` and nothing safer.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

#: Raised from the 64 MiB default. Memory cost is what makes GPU cracking
#: expensive, and 128 MiB per verification is affordable for a login rate
#: measured in tens per minute.
MEMORY_COST_KIB = 131_072
TIME_COST = 3
PARALLELISM = 4

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

#: Not a dictionary - just the handful that appear at the top of every breach
#: corpus, lowercased and compared after stripping.
COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "qwerty",
        "qwerty123",
        "letmein",
        "welcome",
        "welcome1",
        "iloveyou",
        "admin",
        "administrator",
        "changeme",
        "secret",
        "123456",
        "1234567",
        "12345678",
        "123456789",
        "1234567890",
        "abc123",
        "monkey",
        "dragon",
        "football",
        "baseball",
        "sunshine",
        "princess",
        "trustno1",
    }
)

#: What gets appended to a dictionary word to make it "complex".
_DECORATION = "0123456789!@#$%^&*()-_=+.,?~ "

_hasher = PasswordHasher(
    time_cost=TIME_COST, memory_cost=MEMORY_COST_KIB, parallelism=PARALLELISM
)


class WeakPasswordError(ValueError):
    """The password was refused before it was ever hashed."""


def validate_password(password: str) -> None:
    """Raise `WeakPasswordError` if this password should not be accepted."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        # A megabyte password is a denial-of-service attempt on the hasher, not
        # a security-conscious user.
        raise WeakPasswordError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")
    folded = password.strip().lower()
    # `password123!` is `password` with the decoration people add to satisfy a
    # composition rule, and it is guessed in exactly that form. Strip the
    # trailing digits and symbols before the lookup so the decoration does not
    # launder a listed password.
    core = folded.rstrip(_DECORATION)
    if folded in COMMON_PASSWORDS or core in COMMON_PASSWORDS:
        raise WeakPasswordError("password is among the most commonly used; choose another")
    if len(set(password)) < 5:
        raise WeakPasswordError("password repeats too few distinct characters")


def hash_password(password: str) -> str:
    """Validate, then hash. Callers never hash an unvalidated password."""
    validate_password(password)
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Whether the password matches. Never raises on a wrong password."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception:
        # A malformed or foreign hash is a failed login, not a 500. The row is
        # unusable either way and the user should be told the same thing they
        # would be told about a wrong password.
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return True


__all__ = [
    "COMMON_PASSWORDS",
    "MAX_PASSWORD_LENGTH",
    "MEMORY_COST_KIB",
    "MIN_PASSWORD_LENGTH",
    "PARALLELISM",
    "TIME_COST",
    "WeakPasswordError",
    "hash_password",
    "needs_rehash",
    "validate_password",
    "verify_password",
]
