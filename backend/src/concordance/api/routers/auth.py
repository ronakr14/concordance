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

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
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
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestId,
    ip: ClientIp,
) -> TokenOut:
    """Exchange credentials for an access and refresh token."""
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
    return TokenOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_at=pair.expires_at,
    )


@router.post("/refresh", response_model=TokenOut)
def refresh(
    body: RefreshIn,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestId,
) -> TokenOut:
    """Rotate a refresh token: the presented one is revoked as the new pair is issued."""
    try:
        pair = service.refresh(
            session, settings, refresh_token=body.refresh_token, request_id=request_id
        )
    except service.InvalidRefreshTokenError as exc:
        session.commit()  # a reuse detection revoked this user's tokens; keep that
        raise UnauthorizedError(str(exc), code="invalid_refresh_token") from exc

    session.commit()
    return TokenOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_at=pair.expires_at,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    body: RefreshIn,
    session: SessionDep,
    request_id: RequestId,
) -> None:
    """Revoke one refresh token. Unknown tokens succeed silently.

    No authentication required, and no error for an unrecognised token: a client
    logging out with an expired session should not be told to log in first, and
    an attacker learns nothing from either answer.
    """
    service.logout(session, refresh_token=body.refresh_token, request_id=request_id)
    session.commit()


@router.get("/me", response_model=UserOut, dependencies=[Depends(bearer_scheme)])
def me(user: CurrentUser) -> UserOut:
    """The authenticated caller."""
    return UserOut.model_validate(user)


__all__ = ["router"]
