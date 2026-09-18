"""The dependencies every router shares: a session, a caller, and a role check.

`current_user` is where authentication actually happens, and it deliberately
re-reads the user row rather than trusting the token's claims alone. The token
says who signed in and what role they had when they did; the row says whether
that is still true. Without the lookup, deactivating an account would leave the
holder of a valid access token working normally until it expired.

`require_role` is a dependency factory rather than a check inside each handler,
because a permission that is enforced in the handler body is a permission
somebody will forget to write. As a dependency it is visible in the signature
and it shows up in the OpenAPI schema.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from concordance.api.errors import ForbiddenError, UnauthorizedError
from concordance.audit.service import Actor
from concordance.auth.tokens import TokenError, decode_access_token
from concordance.config import Settings, get_settings
from concordance.db.models import User
from concordance.db.repositories.users import UserRepository
from concordance.db.session import get_sessionmaker

#: `auto_error=False` so a missing header raises our envelope rather than
#: Starlette's bare `{"detail": "Not authenticated"}`.
bearer_scheme = HTTPBearer(auto_error=False)


def settings_dep(request: Request) -> Settings:
    """The settings the app was built with - not whatever the process has ambient.

    `create_app(settings)` exists so a test can build an app against its own
    database and secret; reading `get_settings()` here would quietly ignore that
    and use the environment's instead.
    """
    return getattr(request.app.state, "settings", None) or get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


def db_session(settings: SettingsDep) -> Iterator[Session]:
    """A session per request, committed by the route that decides it succeeded.

    Not `session_scope`: a request handler that raises after writing should not
    have those writes committed, and a handler that succeeds knows when it is
    done. Routers call `session.commit()` explicitly.
    """
    session = get_sessionmaker(settings)()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(db_session)]


def current_user(
    request: Request,
    settings: SettingsDep,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """The authenticated caller, or `UnauthorizedError`."""
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("authentication required")

    try:
        claims = decode_access_token(settings, credentials.credentials)
    except TokenError as exc:
        raise UnauthorizedError(str(exc)) from exc

    user = UserRepository(session).get(claims.user_id)
    if user is None or not user.is_active:
        # The token is valid but the account behind it is not usable any more.
        raise UnauthorizedError("account is not active")

    # Handy for the audit writer, which should not have to re-decode anything.
    request.state.user = user
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_role(*roles: str) -> Callable[[User], User]:
    """A dependency that admits only these roles."""

    def guard(user: CurrentUser) -> User:
        if user.role not in roles:
            raise ForbiddenError(
                f"this action requires the {' or '.join(roles)} role",
                details={"required": list(roles), "actual": user.role},
            )
        return user

    return guard


def require_admin(user: CurrentUser) -> User:
    if user.role != "admin":
        raise ForbiddenError(
            "this action requires the admin role",
            details={"required": ["admin"], "actual": user.role},
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


RequestId = Annotated[str | None, Depends(request_id)]


def client_ip(request: Request) -> str | None:
    """The caller's address, if it is one.

    `request.client.host` is not always an IP: behind a proxy on a Unix socket
    it is a path, and under the test client it is `testclient`. The audit
    column is `inet`, so a non-address there fails the insert - and with it the
    login that was being audited. An unknown address is recorded as unknown.
    """
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


ClientIp = Annotated[str | None, Depends(client_ip)]


def current_actor(user: CurrentUser, rid: RequestId, ip: ClientIp) -> Actor:
    """The caller as the audit log will record them."""
    return Actor.of(user, request_id=rid, ip=ip)


def admin_actor(user: AdminUser, rid: RequestId, ip: ClientIp) -> Actor:
    return Actor.of(user, request_id=rid, ip=ip)


#: Any authenticated user. Analysts and admins are the only roles there are.
ActorDep = Annotated[Actor, Depends(current_actor)]
#: An admin, or a 403 before the handler runs.
AdminActor = Annotated[Actor, Depends(admin_actor)]


__all__ = [
    "ActorDep",
    "AdminActor",
    "AdminUser",
    "ClientIp",
    "CurrentUser",
    "RequestId",
    "SessionDep",
    "SettingsDep",
    "admin_actor",
    "bearer_scheme",
    "client_ip",
    "current_actor",
    "current_user",
    "db_session",
    "request_id",
    "require_admin",
    "require_role",
    "settings_dep",
]
