"""Per-request plumbing: a correlation id, a timing log, and a login rate limit.

**The correlation id** is generated per request, bound into the log context, and
returned as `X-Request-Id`. Every audit row written during that request carries
it, so "what happened when the user clicked approve" is one query rather than a
reconstruction from timestamps.

**The rate limiter is in-process and deliberately small.** It exists for one
endpoint - `POST /auth/login` - where unlimited attempts are the whole attack.
A fixed window per client address, held in memory, is honest about what it is:
it does not survive a restart and it does not coordinate across replicas. Both
of those would need Redis, which the project rules exclude, and neither changes
what this is for: making credential stuffing slow enough to be pointless against
a single instance. Postgres could hold the counter, but a write per login
attempt is a worse trade than the limitation above.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from concordance.api.errors import error_body
from concordance.logging_setup import get_logger, new_correlation_id

log = get_logger("api.request")

#: Attempts allowed per window, per client address, on the login endpoint.
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 60.0

#: Paths the limiter guards. Everything else is authenticated and rate-limited
#: by having a token at all.
RATE_LIMITED_PATHS = ("/auth/login",)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Correlation id in, correlation id out, and one log line per request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation = new_correlation_id(request.headers.get("X-Request-Id"))
        request.state.request_id = correlation

        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        response.headers["X-Request-Id"] = correlation
        log.info(
            "api.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round(elapsed_ms, 1),
        )
        return response


class LoginRateLimitMiddleware(BaseHTTPMiddleware):
    """A fixed window per client address on the login endpoint."""

    def __init__(
        self,
        app: object,
        *,
        max_attempts: int = LOGIN_MAX_ATTEMPTS,
        window_seconds: float = LOGIN_WINDOW_SECONDS,
        paths: tuple[str, ...] = RATE_LIMITED_PATHS,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.paths = paths
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._guards(request):
            return await call_next(request)

        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()

        if len(hits) >= self.max_attempts:
            retry_after = int(self.window_seconds - (now - hits[0])) + 1
            log.warning("api.rate_limited", path=request.url.path, client=key)
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content=error_body(
                    "rate_limited", "too many attempts; try again shortly", {"retry_after": retry_after}
                ),
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)
        return await call_next(request)

    def _guards(self, request: Request) -> bool:
        return request.method == "POST" and request.url.path.endswith(self.paths)


__all__ = [
    "LOGIN_MAX_ATTEMPTS",
    "LOGIN_WINDOW_SECONDS",
    "RATE_LIMITED_PATHS",
    "LoginRateLimitMiddleware",
    "RequestContextMiddleware",
]
