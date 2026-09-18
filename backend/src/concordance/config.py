"""Settings.

The only place in the package permitted to read the environment. Everything
else takes a ``Settings`` instance, which makes configuration testable and
keeps the matching engine free of ambient state.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Every knob the system has, with the stage that first uses it noted."""

    # `.env` is addressed absolutely, not as a bare relative name. Alembic runs
    # from `backend/`, the worker may run from anywhere, and a CWD-relative
    # env_file silently finds nothing rather than failing - which looks exactly
    # like a setting the user forgot to write.
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- active now (Stage 0 onward) ---------------------------------------
    ENV: Literal["development", "test", "production"] = "development"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    DATA_DIR: Path = _REPO_ROOT / "data"
    REPORTS_DIR: Path = _REPO_ROOT / "reports"
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    STORAGE_LOCAL_PATH: Path = _REPO_ROOT / "data" / "storage"
    MAX_CANDIDATES_PER_RECORD: int = Field(default=50, ge=1, le=1000)
    TARGET_PRECISION: float = Field(default=0.99, gt=0.0, le=1.0)
    RANDOM_SEED: int = 20260914

    # --- reserved for later stages; optional until then --------------------
    DATABASE_URL: str | None = None  # Stage 5
    # The least-privilege role the application and the API connect as. The
    # owning role in DATABASE_URL runs migrations and owns the tables; an owner
    # can never be revoked from its own table, so proving `audit_logs` is
    # append-only requires a second, non-owning role.
    APP_DATABASE_URL: str | None = None  # Stage 5
    # Seconds to wait for a connection before giving up. Deliberately short:
    # a database that is down should be reported, not waited on.
    DB_CONNECT_TIMEOUT: int = Field(default=5, ge=1, le=60)
    #: Server-side ceiling on a single statement, in seconds; 0 disables it. A
    #: hosted database that stops answering should surface as an error on the
    #: statement rather than as a run that never ends.
    DB_STATEMENT_TIMEOUT: int = Field(default=180, ge=0, le=3600)
    #: Rows per INSERT statement. Bounds how large a single statement's payload
    #: can get, which is what keeps a slow link from stalling mid-statement.
    DB_INSERT_PAGE_SIZE: int = Field(default=50, ge=1, le=1000)
    JWT_SECRET: str | None = None  # Stage 7
    JWT_ACCESS_TTL: int = 900  # Stage 7, seconds
    JWT_REFRESH_TTL: int = 1_209_600  # Stage 7, seconds
    DEFAULT_CASE_MONTHS: int = 3  # Stage 7
    #: Comma-separated origins the browser may call the API from. Not a
    #: wildcard: with credentials allowed, a wildcard would let any page spend
    #: a logged-in user's token.
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"
    #: Path the refresh cookie is scoped to, as the *browser* sees it. The web
    #: app reaches the API through an `/api` prefix (Vite's proxy in
    #: development, nginx in the container), so the cookie is sent to the auth
    #: routes behind it and to nothing else.
    REFRESH_COOKIE_PATH: str = "/api/auth"  # Stage 8
    #: Largest sanction workbook accepted, in bytes. The monthly LEIE file is
    #: about 12 MB as xlsx; the ceiling leaves room for it and refuses the rest
    #: before it is read into memory.
    MAX_UPLOAD_BYTES: int = Field(default=25 * 1024 * 1024, ge=1024)
    #: Data rows accepted from one file. Counted while reading, so an oversized
    #: file is refused at the first row past the cap.
    MAX_UPLOAD_ROWS: int = Field(default=100_000, ge=1)

    # --- Stage 4: the LLM layer -------------------------------------------
    OPENROUTER_API_KEY: str | None = None
    GROQ_API_KEY: str | None = None
    # Groq leads because its limits are per-minute - a backoff can wait one out.
    # OpenRouter is the fallback: it brokers models that go briefly unavailable
    # through no fault of the key, which is tolerable second and not first.
    LLM_PROVIDER_CHAIN: str = "groq,openrouter"
    # Blank means each provider keeps its own default model, which is what
    # allows a chain to span two vendors that do not share model ids. Setting it
    # pins the whole chain; the per-provider settings below override it, because
    # two vendors share no model vocabulary and a global pin sends one vendor's
    # model id to the other.
    LLM_MODEL: str | None = None
    GROQ_MODEL: str | None = None
    OPENROUTER_MODEL: str | None = None
    LLM_ENABLED: bool = False
    LLM_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0, le=300.0)
    LLM_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    # The completion allowance, and the reserve `fits_budget` keeps clear of the
    # context window. Sized for a reasoning model: a model that thinks before it
    # answers spends most of this budget on hidden tokens, and a truncated answer
    # does not come back as a short answer - Groq rejects the truncated JSON with
    # a 400 `json_validate_failed`, which reads like a prompt bug and is not one.
    LLM_MAX_TOKENS: int = Field(default=2_000, ge=256, le=32_000)
    LLM_TOP_K: int = Field(default=3, ge=1, le=10)
    LLM_CACHE_DIR: Path | None = None
    LLM_PRICE_TABLE: Path | None = None
    # Explicit CA bundle for a TLS-inspecting corporate proxy. Blank uses the
    # OS trust store when `truststore` is installed. Verification is never
    # disabled - see `llm/tls.py`.
    LLM_CA_BUNDLE: Path | None = None

    @property
    def generated_dir(self) -> Path:
        """Where the synthetic dataset lands."""
        return self.DATA_DIR / "generated"

    @property
    def llm_cache_dir(self) -> Path:
        """Where `FileCache` writes.

        Defaults to `.cache/llm/` at the repository root rather than under
        `DATA_DIR`: the cache is not part of a dataset, it survives a `data/`
        wipe, and it is already gitignored at that path.
        """
        return self.LLM_CACHE_DIR or (_REPO_ROOT / ".cache" / "llm")

    @field_validator("DATA_DIR", "REPORTS_DIR", "STORAGE_LOCAL_PATH")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v if v.is_absolute() else (_REPO_ROOT / v).resolve()

    @field_validator("LLM_CACHE_DIR", "LLM_PRICE_TABLE", "LLM_CA_BUNDLE")
    @classmethod
    def _absolute_optional(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        return v if v.is_absolute() else (_REPO_ROOT / v).resolve()

    def require_production_secrets(self) -> None:
        """Fail fast rather than boot a production process half-configured."""
        if self.ENV != "production":
            return
        missing = [
            name
            for name in ("DATABASE_URL", "JWT_SECRET")
            if getattr(self, name) in (None, "")
        ]
        if self.LLM_ENABLED and not (self.OPENROUTER_API_KEY or self.GROQ_API_KEY):
            missing.append("OPENROUTER_API_KEY or GROQ_API_KEY")
        if missing:
            raise RuntimeError(
                "ENV=production but these settings are unset: " + ", ".join(missing)
            )

    def public_dict(self) -> dict[str, object]:
        """Config safe to echo into a log line — secrets redacted."""
        secret = {
            "JWT_SECRET",
            "OPENROUTER_API_KEY",
            "GROQ_API_KEY",
            "DATABASE_URL",
            "APP_DATABASE_URL",
        }
        out: dict[str, object] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            out[name] = ("***" if value else None) if name in secret else value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once."""
    s = Settings()
    s.require_production_secrets()
    return s
