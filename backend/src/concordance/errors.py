"""Errors the workflow services raise, without knowing they will become HTTP.

The services behind the API - upload, review, cases - are callable from the CLI
and the tests as well, so they cannot raise FastAPI exceptions. They raise
these, each carrying a stable `code` and per-field `details`, and the API's
error handler maps the class to a status. The mapping lives in one place
(`api/errors.py`); the reason for the refusal lives here, next to the rule.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code = "invalid"

    def __init__(
        self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    """The request is well-formed but the current state does not allow it."""

    code = "conflict"


class InvalidError(DomainError):
    """The request itself is wrong - a mapping missing a field, a bad choice."""

    code = "validation_error"


class TooLargeError(DomainError):
    code = "payload_too_large"


class ForbiddenError(DomainError):
    code = "forbidden"


__all__ = [
    "ConflictError",
    "DomainError",
    "ForbiddenError",
    "InvalidError",
    "NotFoundError",
    "TooLargeError",
]
