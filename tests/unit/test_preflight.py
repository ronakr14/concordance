"""The startup preflight: what it refuses to start, and what it says about it."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from concordance.config import Settings
from concordance.ops import preflight as pf

pytestmark = pytest.mark.unit


def _settings(**overrides: Any) -> Settings:
    base = {
        "DATABASE_URL": "postgresql+psycopg://owner@example/db",
        "APP_DATABASE_URL": "postgresql+psycopg://app@example/db",
        "JWT_SECRET": "a-real-secret",
    }
    return Settings(**{**base, **overrides})


# --- the environment check ------------------------------------------------


def test_a_complete_environment_passes() -> None:
    check = pf.check_env(_settings())
    assert check.ok
    assert "3 required keys" in check.detail


def test_an_unset_key_is_named_and_explained() -> None:
    check = pf.check_env(_settings(JWT_SECRET=None))
    assert not check.ok
    assert "JWT_SECRET" in check.detail
    # The hint must say what the key is for. A preflight that says only
    # "FAIL environment" leaves the reader to go and find out.
    assert "log in" in check.hint


def test_a_placeholder_left_from_the_example_file_is_caught() -> None:
    """`cp .env.example .env` and nothing else is a failure, not a pass."""
    check = pf.check_env(_settings(JWT_SECRET="changeme"))
    assert not check.ok
    assert "placeholder" in check.detail


def test_several_missing_keys_are_all_reported_at_once() -> None:
    check = pf.check_env(_settings(JWT_SECRET=None, APP_DATABASE_URL=None))
    assert not check.ok
    assert "JWT_SECRET" in check.detail
    assert "APP_DATABASE_URL" in check.detail


# --- the port check -------------------------------------------------------


def test_a_free_port_reads_as_free() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    # The socket is closed, so nothing holds the port now.
    (check,) = pf.check_ports([("api", free_port)])
    assert check.ok


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_a_held_port_is_caught_on_either_loopback_family(family: int) -> None:
    """The IPv6 case is the one that got through and broke a start.

    Vite binds `::1` and not `127.0.0.1`. An IPv4-only probe called the port
    free, the preflight passed, and the web server then died on
    `Port 5173 is already in use` - the exact failure the check exists to
    prevent.
    """
    address = "127.0.0.1" if family == socket.AF_INET else "::1"
    try:
        holder = socket.socket(family, socket.SOCK_STREAM)
    except OSError:  # pragma: no cover - a host with IPv6 disabled
        pytest.skip("address family unavailable")
    with holder:
        try:
            holder.bind((address, 0))
        except OSError:  # pragma: no cover - loopback address unavailable
            pytest.skip(f"{address} unavailable")
        holder.listen(1)
        port = holder.getsockname()[1]
        (check,) = pf.check_ports([("web", port)])
    assert not check.ok
    assert "in use" in check.detail


# --- the web dependency check ---------------------------------------------


def test_missing_node_modules_is_a_failure_with_the_command_to_fix_it(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    check = pf.check_web_deps(tmp_path)
    assert not check.ok
    assert "npm ci" in check.hint


def test_a_checkout_with_no_frontend_is_not_a_failure(tmp_path: Path) -> None:
    assert pf.check_web_deps(tmp_path).ok


# --- the report as a whole ------------------------------------------------


def test_a_broken_environment_skips_the_checks_that_depend_on_it(monkeypatch: Any) -> None:
    """One cause must not be reported as three separate problems.

    If the database and migration checks ran anyway they would fail too, and
    the reader would go looking for three faults where there is one.
    """
    called: list[str] = []
    monkeypatch.setattr(pf, "check_database", lambda *_: called.append("db"))
    monkeypatch.setattr(pf, "check_migrations", lambda *_: called.append("mig"))

    report = pf.run_preflight(_settings(JWT_SECRET=None))

    assert called == []
    assert not report.ok
    names = [c.name for c in report.checks]
    assert names[:3] == ["environment", "database", "migrations"]
    assert all("not checked" in c.detail for c in report.checks[1:3])


def test_an_unreachable_database_skips_the_migration_check(monkeypatch: Any) -> None:
    monkeypatch.setattr(pf, "check_database", lambda *_: pf.Check("database", False, "no answer"))
    called: list[str] = []
    monkeypatch.setattr(pf, "check_migrations", lambda *_: called.append("mig"))

    report = pf.run_preflight(_settings())

    assert called == []
    assert not report.ok
    assert "unreachable" in report.checks[2].detail


def test_every_failure_is_reported_not_only_the_first(monkeypatch: Any) -> None:
    monkeypatch.setattr(pf, "check_database", lambda *_: pf.Check("database", False, "no answer"))
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        report = pf.run_preflight(_settings(), ports=[("api", port)])
    assert {c.name for c in report.failures} >= {"database", "migrations", f"port {port}"}


def test_a_passing_report_is_ok() -> None:
    report = pf.PreflightReport([pf.Check("a", True), pf.Check("b", True)])
    assert report.ok
    assert report.failures == []


def test_the_rendered_line_says_which_way_it_went() -> None:
    assert pf.Check("ports", True, "free").line().startswith("ok  ")
    assert pf.Check("ports", False, "held").line().startswith("FAIL")
