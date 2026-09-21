"""The API's contract, checked without a database.

Authentication and role checks run before any handler touches the database, so
the refusals - no token, an expired token, an analyst on an admin route - can be
proved against an app whose database URL points nowhere. If one of these ever
needs a live database to pass, the check has moved into a handler body, which
is exactly what the dependency design is there to prevent.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from concordance.auth.tokens import ALGORITHM, issue_access_token
from concordance.config import Settings

pytestmark = pytest.mark.unit

SECRET = "contract-test-signing-key-long-enough-to-be-plausible"

#: Every route and method, with a path whose ids are well-formed.
_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def settings() -> Settings:
    # Port 1 on localhost: an engine can be built for it, and nothing answers.
    return Settings(
        JWT_SECRET=SECRET,
        DATABASE_URL="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
        APP_DATABASE_URL="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
        DB_CONNECT_TIMEOUT=1,
    )


@pytest.fixture
def app(settings: Settings) -> Iterator[Any]:
    from concordance.api.app import create_app
    from concordance.db.session import dispose_engine

    dispose_engine()
    yield create_app(settings)
    # The engine is process-wide; leaving this one behind would point the next
    # test that asks for a session at a database that does not exist.
    dispose_engine()


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _protected_routes(app: Any) -> list[tuple[str, str]]:
    open_paths = {"/health", "/auth/login", "/auth/refresh", "/auth/logout", "/auth/register"}
    routes: list[tuple[str, str]] = []
    for path, ops in app.openapi()["paths"].items():
        if path in open_paths:
            continue
        for method in ops:
            routes.append((method.upper(), path.replace("{", "").replace("}", "")))
    return routes


def _concrete(path: str) -> str:
    """Turn `/matches/match_id` back into a path with a real-looking id."""
    parts = [(_ID if p.endswith("_id") else p) for p in path.split("/")]
    return "/".join(parts)


def test_the_openapi_schema_generates_and_every_operation_is_uniquely_named(app: Any) -> None:
    spec = app.openapi()
    ids = [op["operationId"] for ops in spec["paths"].values() for op in ops.values()]
    assert len(ids) == len(set(ids))
    assert len(spec["paths"]) >= 30
    # The pagination envelope is one generic model, not a dict.
    page = spec["components"]["schemas"]["Page_MatchListItemOut_"]
    assert set(page["required"]) == {"items", "total", "limit", "offset"}


def test_every_protected_route_refuses_a_request_with_no_token(app: Any, client: TestClient) -> None:
    routes = _protected_routes(app)
    assert len(routes) >= 25
    for method, path in routes:
        response = client.request(method, _concrete(path), json={})
        assert response.status_code == 401, (method, path, response.text)
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_an_expired_token_is_refused(settings: Settings, client: TestClient) -> None:
    token, _ = issue_access_token(
        settings,
        user_id=uuid.uuid4(),
        email="someone@example.test",
        role="admin",
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    response = client.get("/matches", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "token has expired"


def test_a_forged_token_is_refused(client: TestClient) -> None:
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "role": "admin", "type": "access",
         "iat": int(datetime.now(UTC).timestamp()),
         "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())},
        "not-the-server-signing-key-but-long-enough-for-hs256",
        algorithm=ALGORITHM,
    )
    response = client.get("/stats/kpis", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def _as(app: Any, role: str) -> Any:
    from concordance.api.deps import current_user
    from concordance.db.models import User

    user = User(id=uuid.uuid4(), email=f"{role}@example.test", password_hash="x", role=role,
                is_active=True)
    app.dependency_overrides[current_user] = lambda: user
    return user


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"/matches/{_ID}/approve", {}),
        ("POST", f"/cases/{_ID}/close", {"reason": "resolved elsewhere"}),
        ("POST", "/cases", {"match_result_id": _ID}),
        ("GET", "/audit", None),
    ],
)
def test_an_analyst_is_refused_the_admin_routes_before_the_handler_runs(
    app: Any, client: TestClient, method: str, path: str, body: Any
) -> None:
    _as(app, "analyst")
    response = client.request(method, path, json=body)
    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "forbidden"
    assert error["details"] == {"required": ["admin"], "actual": "analyst"}


def test_validation_errors_use_the_envelope_with_per_field_details(
    app: Any, client: TestClient
) -> None:
    _as(app, "admin")
    response = client.post(f"/cases/{_ID}/close", json={"reason": ""})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert "reason" in error["details"]


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "offset=-1"])
def test_page_bounds_are_enforced_at_the_edge(app: Any, client: TestClient, query: str) -> None:
    _as(app, "analyst")
    response = client.get(f"/matches?{query}")
    assert response.status_code == 422


def test_an_unknown_route_is_a_not_found_envelope(client: TestClient) -> None:
    response = client.get("/no/such/thing")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_cors_admits_the_dev_origin_and_nothing_else(client: TestClient) -> None:
    allowed = client.options(
        "/health",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    refused = client.options(
        "/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in refused.headers


def test_login_is_rate_limited_per_client(client: TestClient) -> None:
    from concordance.api.middleware import LOGIN_MAX_ATTEMPTS

    # Malformed bodies: refused at validation, so no database is needed, but
    # they still count - the limiter runs before the handler.
    codes = [
        client.post("/auth/login", json={"email": "not-an-email", "password": "x"}).status_code
        for _ in range(LOGIN_MAX_ATTEMPTS + 1)
    ]
    assert codes[:LOGIN_MAX_ATTEMPTS] == [422] * LOGIN_MAX_ATTEMPTS
    assert codes[-1] == 429


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    echoed = client.get("/no/such/thing", headers={"X-Request-Id": "trace-me"})
    assert echoed.headers["X-Request-Id"] == "trace-me"
    assert client.get("/no/such/thing").headers["X-Request-Id"]


def test_an_unexpected_failure_leaks_nothing_about_the_server(
    client: TestClient, settings: Settings
) -> None:
    """A valid token gets past authentication and into a database that is not
    there. The driver's error names the host, port and user; none of it, and no
    traceback or file path, may reach the caller."""
    token, _ = issue_access_token(
        settings, user_id=uuid.uuid4(), email="a@example.com", role="admin"
    )
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "something went wrong"}}
    for leak in ("Traceback", "File \"", ".py", "psycopg", "127.0.0.1", "nobody", "sqlalchemy"):
        assert leak not in response.text, leak
