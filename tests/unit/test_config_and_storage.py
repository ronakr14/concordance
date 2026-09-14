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
def test_unbuilt_commands_name_their_stage() -> None:
    result = runner.invoke(app, ["match", "fit"])
    assert result.exit_code == 2
    assert "Stage 3" in result.stdout
