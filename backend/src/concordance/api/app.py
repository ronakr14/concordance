"""The FastAPI application.

A factory rather than a module-level singleton: the tests build an app with
their own settings, and `create_app()` makes that a parameter instead of a
monkeypatch. `uvicorn` gets `concordance.api.app:app`, which is the factory's
output with the ambient settings.

CORS admits the configured dev origin and nothing else. A wildcard would be
convenient and would also let any page in the browser spend a logged-in user's
token.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from concordance import __version__
from concordance.api.errors import install_error_handlers
from concordance.api.middleware import LoginRateLimitMiddleware, RequestContextMiddleware
from concordance.api.routers import audit_stats, cases, matches, reconciliation, sanctions
from concordance.api.routers import auth as auth_router
from concordance.api.schemas import HealthOut
from concordance.config import Settings, get_settings
from concordance.logging_setup import configure_logging, get_logger

log = get_logger("api.app")

DESCRIPTION = """
Provider sanctions and exclusions reconciliation.

Matches sanction records against a provider master file with a Fellegi-Sunter
engine, EM-learned weights and isotonic calibration, and routes the grey band to
an LLM adjudicator. Every run is pinned to a data snapshot and can be replayed.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(resolved.LOG_LEVEL, json_logs=resolved.ENV == "production")

    app = FastAPI(
        title="Concordance",
        description=DESCRIPTION.strip(),
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = resolved

    # Order matters: the rate limiter should reject before anything else runs,
    # and the context middleware should still stamp the response it returns.
    app.add_middleware(LoginRateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(resolved),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )

    install_error_handlers(app)
    app.include_router(auth_router.router)
    app.include_router(sanctions.router)
    app.include_router(reconciliation.router)
    app.include_router(matches.router)
    app.include_router(cases.router)
    app.include_router(audit_stats.audit_router)
    app.include_router(audit_stats.stats_router)

    @app.get("/health", response_model=HealthOut, tags=["meta"])
    def health() -> HealthOut:
        """Liveness plus whether the database answers. Never authenticated."""
        from concordance.db.session import ping

        reachable = bool(resolved.DATABASE_URL) and ping(resolved)
        return HealthOut(
            status="ok" if reachable else "degraded",
            version=__version__,
            database=reachable,
        )

    log.info("api.created", env=resolved.ENV, version=__version__)
    return app


def _cors_origins(settings: Settings) -> list[str]:
    raw = str(settings.CORS_ORIGINS or "").strip()
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def __getattr__(name: str) -> Any:
    """`concordance.api.app:app` for uvicorn, without building one on import.

    Importing this module must not construct an application - the CLI imports
    half the package to print `--help` - so the singleton is created on first
    attribute access instead.
    """
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)


__all__ = ["create_app"]
