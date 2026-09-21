"""Startup preflight.

`make up` spawns three processes. Without a preflight, a misconfigured checkout
spawns all three and each fails in its own way a few seconds apart: the API
reports a refused connection, the worker reports the same thing in a different
format, and the web server comes up fine and serves a page whose every request
fails. The reader gets three unrelated errors and none of them says "you have
not set DATABASE_URL".

So the checks run first, in one process, and name the problem once. Every check
carries a `hint` saying what to do about it, because a check that only says
`FAIL migrations` is one the reader still has to diagnose.

Each check returns a `Check` rather than raising, so one failure does not hide
the four behind it - a fresh checkout is usually missing several things at
once, and reporting them one run at a time is several runs.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from concordance.config import Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

#: Settings with no usable default, which every process needs before it starts.
REQUIRED_KEYS: tuple[tuple[str, str], ...] = (
    ("DATABASE_URL", "the owning Postgres role; runs migrations and owns the tables"),
    ("APP_DATABASE_URL", "the least-privilege role the API and worker connect as"),
    ("JWT_SECRET", "signs access tokens; without it nobody can log in"),
)

#: Values that mean "copied from .env.example and never filled in".
PLACEHOLDERS = {"changeme", "change-me", "replace-me", "xxx"}


@dataclass(frozen=True)
class Check:
    """One preflight result. `ok` false is a refusal to start, not a warning."""

    name: str
    ok: bool
    detail: str = ""
    hint: str = ""

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        return f"{mark} {self.name}" + (f" - {self.detail}" if self.detail else "")


@dataclass(frozen=True)
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------


def check_env(settings: Settings) -> Check:
    """Every required key is set, and none is still the `.env.example` stub."""
    missing = [key for key, _ in REQUIRED_KEYS if not getattr(settings, key, None)]
    stubs = [
        key
        for key, _ in REQUIRED_KEYS
        if str(getattr(settings, key, "") or "").strip().lower() in PLACEHOLDERS
    ]
    if not missing and not stubs:
        return Check("environment", True, f"{len(REQUIRED_KEYS)} required keys set")
    parts = []
    if missing:
        parts.append("unset: " + ", ".join(missing))
    if stubs:
        parts.append("still the example placeholder: " + ", ".join(stubs))
    described = dict(REQUIRED_KEYS)
    hint = "; ".join(f"{k} is {described[k]}" for k in [*missing, *stubs])
    return Check("environment", False, "; ".join(parts), f"set it in .env - {hint}")


def check_database(settings: Settings) -> Check:
    """Both roles answer a `SELECT 1` inside `DB_CONNECT_TIMEOUT`.

    The app role is checked as well as the owner, because the owner is only
    used by migrations: a checkout whose `APP_DATABASE_URL` is wrong migrates
    cleanly and then fails on the first request.
    """
    from sqlalchemy import create_engine, text

    if not settings.DATABASE_URL:
        return Check("database", False, "DATABASE_URL is not set", "set DATABASE_URL in .env")
    for label, url in (("owner", settings.DATABASE_URL), ("app", settings.APP_DATABASE_URL)):
        if not url:
            continue
        engine = create_engine(
            url, connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT}, pool_pre_ping=True
        )
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            return Check(
                "database",
                False,
                f"the {label} role did not answer: {_short(exc)}",
                "check the host is reachable and the credentials are current; a Neon "
                "branch that has been idle can take a moment to wake",
            )
        finally:
            engine.dispose()
    return Check("database", True, "both roles answer")


def check_migrations(settings: Settings, alembic_ini: Path | None = None) -> Check:
    """The database is on the same revision as the newest script on disk."""
    # Silenced before the import, not after: Alembic registers its plugins on
    # import and narrates each one at INFO, so a logger quietened afterwards is
    # quietened too late.
    _quiet("alembic")

    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    ini = alembic_ini or _default_alembic_ini()
    if not ini.is_file():
        return Check("migrations", False, f"alembic.ini not found at {ini}", "run from a checkout")
    heads = set(ScriptDirectory.from_config(Config(str(ini))).get_heads())
    engine = create_engine(
        str(settings.DATABASE_URL), connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT}
    )
    try:
        with engine.connect() as conn:
            current = set(MigrationContext.configure(conn).get_current_heads())
    except Exception as exc:
        return Check("migrations", False, f"unreadable: {_short(exc)}", "fix the database first")
    finally:
        engine.dispose()
    if current == heads:
        return Check("migrations", True, f"at {', '.join(sorted(heads)) or 'base'}")
    return Check(
        "migrations",
        False,
        f"database at {', '.join(sorted(current)) or 'base'}, "
        f"scripts at {', '.join(sorted(heads))}",
        "run `concordance db upgrade head` (which `make up` does for you)",
    )


def check_ports(ports: Sequence[tuple[str, int]]) -> list[Check]:
    """Each port is free. A port already held is usually a previous `make up`."""
    out = []
    for name, port in ports:
        busy = _port_in_use(port)
        out.append(
            Check(
                f"port {port}",
                not busy,
                f"{name} - {'in use' if busy else 'free'}",
                f"stop whatever holds {port}, or run `make down` if an earlier start is up",
            )
        )
    return out


def check_web_deps(frontend: Path) -> Check:
    """`npm ci` has been run, so Vite is not discovered missing three seconds in."""
    if not (frontend / "package.json").is_file():
        return Check("web dependencies", True, "no frontend in this checkout")
    if (frontend / "node_modules").is_dir():
        return Check("web dependencies", True, "node_modules present")
    return Check("web dependencies", False, "node_modules is missing", f"run `npm ci` in {frontend}")


# --------------------------------------------------------------------------
# the whole thing
# --------------------------------------------------------------------------


def run_preflight(
    settings: Settings,
    *,
    ports: Iterable[tuple[str, int]] = (),
    frontend: Path | None = None,
    alembic_ini: Path | None = None,
    migrations: bool = True,
) -> PreflightReport:
    """Run every check, ordered so one cause is not reported as three problems.

    An unset `DATABASE_URL` fails the database and migration checks too, so a
    failed environment check short-circuits the two that depend on it. They are
    reported as not checked rather than silently dropped, because a report with
    rows missing reads as a report that did less than it says.

    `migrations=False` is for `make up`, which upgrades to head straight after
    this. Checking first would refuse exactly the database the upgrade is about
    to fix: a fresh clone's is at base, and a pull that brings a migration
    leaves it one behind. The row is still printed, saying who does it instead.
    """
    checks = [check_env(settings)]
    if checks[0].ok:
        checks.append(check_database(settings))
        if checks[-1].ok and not migrations:
            checks.append(Check("migrations", True, "not checked - upgraded to head next"))
        elif checks[-1].ok:
            checks.append(check_migrations(settings, alembic_ini))
        else:
            checks.append(Check("migrations", False, "not checked - the database is unreachable"))
    else:
        for name in ("database", "migrations"):
            checks.append(Check(name, False, "not checked - the environment is incomplete"))
    checks.extend(check_ports(list(ports)))
    if frontend is not None:
        checks.append(check_web_deps(frontend))
    return PreflightReport(checks)


def _quiet(logger_name: str) -> None:
    import logging

    logging.getLogger(logger_name).setLevel(logging.WARNING)


def _port_in_use(port: int, host: str = "localhost") -> bool:
    """Is anything listening, on either loopback family?

    Both families, because they are separate listeners. A Vite server left
    over from an earlier start binds `::1` and not `127.0.0.1`, so an IPv4-only
    probe reports the port free and the preflight waves through a start that
    then fails with `Port 5173 is already in use` - which is exactly the class
    of failure this check exists to prevent. Found by that happening.
    """
    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:  # pragma: no cover - a host with no loopback name
        candidates = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))]
    for family, socktype, proto, _canon, addr in candidates:
        with socket.socket(family, socktype, proto) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(addr) == 0:
                return True
    return False


def _default_alembic_ini() -> Path:
    return Path(__file__).resolve().parents[3] / "alembic.ini"


def _short(exc: Exception) -> str:
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else exc.__class__.__name__)[:200]
