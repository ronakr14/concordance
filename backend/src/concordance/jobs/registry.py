"""Which function runs a job of a given kind.

A registry rather than an `if kind == ...` chain for one reason worth naming:
the worker must be able to answer "do I know how to run this?" *before* it
claims a row. A job whose kind nothing handles is a dead letter, not a crash
loop, and it should be recognised as one on the first attempt.

Handlers take the session and settings and return a JSON-serializable summary.
They do not commit: the worker owns the transaction, so a handler that writes
results and then fails leaves nothing behind.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from concordance.config import Settings

#: `(session, settings, payload) -> summary`. The summary is logged and, for a
#: CLI-triggered job, printed; nothing depends on its shape.
Handler = Callable[[Session, Settings, dict[str, Any]], dict[str, Any]]

_HANDLERS: dict[str, Handler] = {}


class UnknownJobKindError(LookupError):
    """No handler is registered for this kind."""


def register(kind: str) -> Callable[[Handler], Handler]:
    def decorate(fn: Handler) -> Handler:
        if kind in _HANDLERS and _HANDLERS[kind] is not fn:
            raise ValueError(f"handler for {kind!r} is already registered")
        _HANDLERS[kind] = fn
        return fn

    return decorate


def handler_for(kind: str) -> Handler:
    try:
        return _HANDLERS[kind]
    except KeyError as exc:
        raise UnknownJobKindError(f"no handler registered for job kind {kind!r}") from exc


def known_kinds() -> list[str]:
    return sorted(_HANDLERS)


def is_known(kind: str) -> bool:
    return kind in _HANDLERS


__all__ = [
    "Handler",
    "UnknownJobKindError",
    "handler_for",
    "is_known",
    "known_kinds",
    "register",
]
