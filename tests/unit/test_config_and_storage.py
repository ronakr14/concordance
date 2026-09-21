"""Stage 0 scaffolding: settings, logging, storage backends, CLI surface."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from concordance import __version__
from concordance.cli import app
from concordance.config import Settings
from concordance.protocols import StorageBackend
from concordance.storage import LocalStorage, S3Storage

runner = CliRunner()


@pytest.mark.unit
def test_settings_defaults_are_usable_without_any_env() -> None:
    s = Settings(_env_file=None)
    assert s.ENV == "development"
    assert s.DATA_DIR.is_absolute()
    assert s.generated_dir.name == "generated"
    assert 0 < s.TARGET_PRECISION <= 1


@pytest.mark.unit
def test_production_requires_secrets() -> None:
    s = Settings(_env_file=None, ENV="production")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        s.require_production_secrets()

    ok = Settings(
        _env_file=None,
        ENV="production",
        DATABASE_URL="postgresql+psycopg://u:p@h/db",
        APP_DATABASE_URL="postgresql+psycopg://app:p@h/db",
        JWT_SECRET="x" * 32,
    )
    ok.require_production_secrets()


@pytest.mark.unit
def test_public_dict_redacts_secrets() -> None:
    s = Settings(_env_file=None, JWT_SECRET="super-secret", GROQ_API_KEY="gsk_live")
    pub = s.public_dict()
    assert pub["JWT_SECRET"] == "***"
    assert pub["GROQ_API_KEY"] == "***"
    assert pub["ENV"] == "development"


@pytest.mark.unit
def test_local_storage_roundtrip(tmp_path: Path) -> None:
    store: StorageBackend = LocalStorage(tmp_path)
    uri = store.put("uploads/a.txt", b"hello")
    assert store.exists(uri)
    assert store.get(uri) == b"hello"
    store.delete(uri)
    assert not store.exists(uri)


@pytest.mark.unit
def test_local_storage_rejects_escaping_keys(tmp_path: Path) -> None:
    store = LocalStorage(tmp_path)
    with pytest.raises(ValueError, match="escapes storage root"):
        store.put("../outside.txt", b"nope")


@pytest.mark.unit
def test_s3_backend_is_a_declared_seam_not_an_implementation() -> None:
    s3 = S3Storage(bucket="concordance")
    assert isinstance(s3, StorageBackend)
    with pytest.raises(NotImplementedError):
        s3.put("k", b"v")


@pytest.mark.unit
def test_cli_exposes_every_command_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in ("data", "match", "llm", "db", "report"):
        assert group in result.stdout


@pytest.mark.unit
def test_cli_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


@pytest.mark.unit
def test_the_placeholder_helper_names_its_stage() -> None:
    """A command for a stage not yet built exits 2 and says which stage.

    `match fit` was once a placeholder, then `llm ping`, then `db upgrade` and
    `db load` - all of them real now, which is the point of the pattern: the
    surface of the finished system is visible from day one and each placeholder
    disappears the moment its stage lands. Stages 0-5 have exhausted the list,
    so the helper itself is what is left to test until Stage 6 registers the
    next batch.
    """
    import typer

    from concordance.cli import _not_until

    with pytest.raises(typer.Exit) as raised:
        _not_until(6, "Run orchestration")
    assert raised.value.exit_code == 2


@pytest.mark.unit
def test_stage_five_commands_are_no_longer_placeholders() -> None:
    """`db upgrade` and `db load` are real, and their help says what they do."""
    result = runner.invoke(app, ["db", "--help"])
    assert result.exit_code == 0
    for command in ("upgrade", "downgrade", "load", "reset", "ping", "import-cache"):
        assert command in result.stdout
    assert "Stage 5" not in result.stdout


@pytest.mark.unit
def test_the_runtime_engine_connects_as_the_app_role_not_the_owner() -> None:
    """The owner can never be revoked from its own table, so `audit_logs` is only
    append-only if the API and worker connect as some other role."""
    from concordance.db.session import database_url

    s = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+psycopg://owner:p@h/db",
        APP_DATABASE_URL="postgresql+psycopg://app:p@h/db",
    )
    assert database_url(s).startswith("postgresql+psycopg://app:")
    assert database_url(s, owner=True).startswith("postgresql+psycopg://owner:")

    # No quiet fallback to the owner when the app role is not configured.
    with pytest.raises(RuntimeError, match="APP_DATABASE_URL"):
        database_url(Settings(_env_file=None, DATABASE_URL="postgresql+psycopg://owner:p@h/db"))
