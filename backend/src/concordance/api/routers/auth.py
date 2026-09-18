"""`/auth` - register, login, refresh, logout, me.

The router is thin on purpose: it translates HTTP into `auth.service` calls and
service errors into the API's envelope. Every security decision - what a failed
login reveals, when a refresh token is revoked, what makes a password acceptable
- lives in the service, where it can be tested without a client.

Registration is gated by `ENV`. In development it is open, because a fresh
checkout needs a first account and there is nobody to grant it. In production it
requires an admin token, so an exposed instance cannot be handed accounts by
whoever finds it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials

from concordance.api.deps import (
    ClientIp,
    CurrentUser,
    RequestId,
    SessionDep,
    SettingsDep,
    bearer_scheme,
    current_user,
)
from concordance.api.errors import ConflictError, ForbiddenError, UnauthorizedError, ValidationError
from concordance.api.schemas import LoginIn, RefreshIn, RegisterIn, TokenOut, UserOut
from concordance.auth import service
from concordance.auth.passwords import WeakPasswordError
from concordance.config import Settings
from concordance.logging_setup import get_logger

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger("api.auth")


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    body: RegisterIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestId,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> UserOut:
    """Create an account. Open in development, admin-only in production."""
    actor = None
    if settings.ENV == "production":
        # Resolved by hand rather than as a dependency, because in development
        # the same endpoint must work with no token at all.
        caller = current_user(request, settings, session, credentials)
        if caller.role != "admin":
            raise ForbiddenError(
                "in production only an admin creates accounts",
                code="registration_closed",
                details={"required": ["admin"], "actual": caller.role},
            )
        actor = caller.id

    try:
        user = service.register(
            session,
            settings,
            email=str(body.email),
            password=body.password,
            full_name=body.full_name,
            role=body.role,
            actor_id=actor,
            request_id=request_id,
        )
    except service.EmailTakenError as exc:
        raise ConflictError(str(exc), code="email_taken") from exc
    except WeakPasswordError as exc:
        raise ValidationError(str(exc), code="weak_password", details={"password": str(exc)}) from exc

    session.commit()
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenOut)
def login(
    body: LoginIn,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestId,
    ip: ClientIp,
) -> TokenOut:
    """Exchange credentials for an access and refresh token.

    With `transport: "cookie"` the refresh token is set as an httpOnly cookie
    and the body's `refresh_token` is null - the browser app's mode.
    """
    try:
        pair = service.login(
            session,
            settings,
            email=str(body.email),
            password=body.password,
            request_id=request_id,
            ip=ip,
        )
    except service.InvalidCredentialsError as exc:
        # Deliberately the same message for every cause. See auth/service.py.
        raise UnauthorizedError("email or password is incorrect", code="invalid_credentials") from exc

    session.commit()
    return _issue(response, settings, pair, cookie=body.transport == "cookie")


@router.post("/refresh", response_model=TokenOut)
def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestId,
    body: RefreshIn | None = None,
) -> TokenOut:
    """Rotate a refresh token: the presented one is revoked as the new pair is issued.

    The token comes from the body or, when the body names none, from the
    refresh cookie; the new one goes back the way the old one came.
    """
    token, from_cookie = _presented(request, body)
    if token is None:
        raise UnauthorizedError("no refresh token presented", code="invalid_refresh_token")
    try:
        pair = service.refresh(session, settings, refresh_token=token, request_id=request_id)
    except service.InvalidRefreshTokenError as exc:
        session.commit()  # a reuse detection revoked this user's tokens; keep that
        if from_cookie:
            raise UnauthorizedError(
                str(exc), code="invalid_refresh_token", headers=_cleared_cookie_headers(settings)
            ) from exc
        raise UnauthorizedError(str(exc), code="invalid_refresh_token") from exc

    session.commit()
    return _issue(response, settings, pair, cookie=from_cookie)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestId,
    body: RefreshIn | None = None,
) -> None:
    """Revoke one refresh token. Unknown tokens succeed silently.

    No authentication required, and no error for an unrecognised token: a client
    logging out with an expired session should not be told to log in first, and
    an attacker learns nothing from either answer. The refresh cookie, if any,
    is cleared either way.
    """
    token, _ = _presented(request, body)
    if token is not None:
        service.logout(session, refresh_token=token, request_id=request_id)
        session.commit()
    _clear_cookie(response, settings)


# --------------------------------------------------------------------------
# the refresh cookie
# --------------------------------------------------------------------------

#: httpOnly, so no script on the page can read it; SameSite=Strict, so no other
#: site can make the browser send it; scoped to the auth routes, so it does not
#: ride along on every API call.
REFRESH_COOKIE = "concordance_refresh"


def _presented(request: Request, body: RefreshIn | None) -> tuple[str | None, bool]:
    """The refresh token and whether it came from the cookie. The body wins."""
    if body is not None and body.refresh_token:
        return body.refresh_token, False
    cookie = request.cookies.get(REFRESH_COOKIE)
    return (cookie, True) if cookie else (None, False)


def _issue(response: Response, settings: Settings, pair: Any, *, cookie: bool) -> TokenOut:
    if not cookie:
        return TokenOut(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_at=pair.expires_at,
        )
    response.set_cookie(
        REFRESH_COOKIE,
        pair.refresh_token,
        max_age=settings.JWT_REFRESH_TTL,
        path=settings.REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.ENV == "production",
        samesite="strict",
    )
    return TokenOut(access_token=pair.access_token, refresh_token=None, expires_at=pair.expires_at)


def _clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=settings.REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.ENV == "production",
        samesite="strict",
    )


def _cleared_cookie_headers(settings: Settings) -> dict[str, str]:
    """The `Set-Cookie` that clears the cookie, for an error response.

    An error handler builds a fresh response, so headers set on the injected
    `Response` are lost when the route raises. A rejected cookie is cleared so
    the browser stops presenting a token that will never work again.
    """
    scratch = Response()
    _clear_cookie(scratch, settings)
    return {"set-cookie": scratch.headers["set-cookie"]}


@router.get("/me", response_model=UserOut, dependencies=[Depends(bearer_scheme)])
def me(user: CurrentUser) -> UserOut:
    """The authenticated caller."""
    return UserOut.model_validate(user)


__all__ = ["router"]
