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

    model_config = SettingsConfigDict(
        env_file=".env",
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
    JWT_SECRET: str | None = None  # Stage 7
    JWT_ACCESS_TTL: int = 900  # Stage 7, seconds
    JWT_REFRESH_TTL: int = 1_209_600  # Stage 7, seconds
    OPENROUTER_API_KEY: str | None = None  # Stage 4
    GROQ_API_KEY: str | None = None  # Stage 4
    LLM_PROVIDER_CHAIN: str = "openrouter,groq"  # Stage 4
    LLM_MODEL: str | None = None  # Stage 4
    LLM_ENABLED: bool = False  # Stage 4
    DEFAULT_CASE_MONTHS: int = 3  # Stage 7

    @property
    def generated_dir(self) -> Path:
        """Where the synthetic dataset lands."""
        return self.DATA_DIR / "generated"

    @field_validator("DATA_DIR", "REPORTS_DIR", "STORAGE_LOCAL_PATH")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
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
        secret = {"JWT_SECRET", "OPENROUTER_API_KEY", "GROQ_API_KEY", "DATABASE_URL"}
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
