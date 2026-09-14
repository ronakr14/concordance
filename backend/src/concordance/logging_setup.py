"""Structured logging.

Human-readable in development, JSON in production, with a correlation id bound
per CLI command so a single run's lines can be pulled out of a shared log.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import MutableMapping
from contextvars import ContextVar
from types import TracebackType
from typing import Any

import structlog

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def _add_correlation_id(
    _logger: object, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict.setdefault("cid", correlation_id.get())
    return event_dict


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """Install the structlog pipeline. Idempotent; safe to call per command."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_correlation_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def new_correlation_id() -> str:
    """Bind a fresh correlation id for this command run and return it."""
    cid = uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


class Progress:
    """Coarse progress reporting for long CLI operations.

    Deliberately not a progress bar: these runs are also driven from a Makefile
    and from CI, where a redrawn bar is noise. One line every ``every`` items.
    """

    def __init__(self, label: str, total: int | None = None, every: int = 10_000) -> None:
        self.label = label
        self.total = total
        self.every = max(1, every)
        self.count = 0
        self._log = get_logger("progress")
        self._start = 0.0

    def __enter__(self) -> Progress:
        self._start = time.perf_counter()
        self._log.info("start", op=self.label, total=self.total)
        return self

    def tick(self, n: int = 1) -> None:
        self.count += n
        if self.count % self.every < n:
            self._log.info(
                "progress",
                op=self.label,
                done=self.count,
                total=self.total,
                elapsed_s=round(time.perf_counter() - self._start, 2),
            )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._log.info(
            "done" if exc is None else "failed",
            op=self.label,
            done=self.count,
            elapsed_s=round(time.perf_counter() - self._start, 2),
        )
