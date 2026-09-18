"""One error shape, for every failure the API can produce.

A client that has to parse three different error formats - FastAPI's
`{"detail": ...}`, a validation error's list of dicts, and whatever a handler
invented - ends up with three code paths and trusts none of them. So everything
leaves here as:

```json
{"error": {"code": "not_found", "message": "no such match", "details": {...}}}
```

`code` is a stable machine-readable token the UI can branch on; `message` is for
a human; `details` carries per-field problems where they exist and is absent
otherwise. Unexpected exceptions become a `internal_error` with no detail at all
- an exception message is as likely to contain a connection string as anything
useful to the caller, and the log already has the traceback.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from concordance import errors as domain
from concordance.logging_setup import get_logger

#: Starlette renamed this constant; the number is the contract, so it is named
#: once here rather than deprecation-warned at every use.
HTTP_422 = 422
HTTP_413 = 413

log = get_logger("api.errors")


class ApiError(Exception):
    """An error with a code, a message, and the status it should produce."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}
        #: Sent with the error response - e.g. a `Set-Cookie` that clears a
        #: refresh cookie the server has just refused.
        self.headers = headers or {}


class NotFoundError(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(ApiError):
    """The request is valid but the state does not allow it - a duplicate approval, a re-upload."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class UnauthorizedError(ApiError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(ApiError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class ValidationError(ApiError):
    status_code = HTTP_422
    code = "validation_error"


class RateLimitedError(ApiError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


#: The workflow services' refusals, as statuses. One table, so "a conflict is a
#: 409" is decided once rather than at every call site.
_DOMAIN_STATUS: dict[type[domain.DomainError], int] = {
    domain.NotFoundError: status.HTTP_404_NOT_FOUND,
    domain.ConflictError: status.HTTP_409_CONFLICT,
    domain.InvalidError: HTTP_422,
    domain.TooLargeError: HTTP_413,
    domain.ForbiddenError: status.HTTP_403_FORBIDDEN,
}


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return body


def install_error_handlers(app: FastAPI) -> None:
    """Route every kind of failure through the one envelope."""

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        headers = dict(exc.headers)
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
            headers=headers or None,
        )

    @app.exception_handler(domain.DomainError)
    async def _domain_error(_request: Request, exc: domain.DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=_DOMAIN_STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST),
            content=error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields: dict[str, Any] = {}
        for error in exc.errors():
            # Drop the leading "body"/"query" segment: the caller knows where
            # they put it, and `body.mapping.npi` reads worse than `mapping.npi`.
            location = ".".join(str(part) for part in error["loc"][1:]) or "request"
            fields[location] = error["msg"]
        return JSONResponse(
            status_code=HTTP_422,
            content=error_body("validation_error", "the request body is not valid", fields),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            status.HTTP_401_UNAUTHORIZED: "unauthorized",
            status.HTTP_403_FORBIDDEN: "forbidden",
            status.HTTP_404_NOT_FOUND: "not_found",
            status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
            status.HTTP_409_CONFLICT: "conflict",
            status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
        }.get(exc.status_code, "error")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The traceback goes to the log; the caller gets nothing but the fact.
        log.exception(
            "api.unhandled",
            path=request.url.path,
            method=request.method,
            error=f"{type(exc).__name__}: {exc}",
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("internal_error", "something went wrong"),
        )


__all__ = [
    "ApiError",
    "ConflictError",
    "ForbiddenError",
    "NotFoundError",
    "RateLimitedError",
    "UnauthorizedError",
    "ValidationError",
    "error_body",
    "install_error_handlers",
]
