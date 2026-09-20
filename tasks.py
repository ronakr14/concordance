"""Task runner.

`make` is awkward on Windows and this project is Windows-first, so the targets
live here instead. Every target is a thin shell over the CLI or a dev tool -
nothing is implemented in this file that the CLI cannot do on its own.

    python tasks.py seed CORRUPTION=0.5 PROVIDERS=50000 SEED=42
    python tasks.py test
    python tasks.py lint fmt typecheck

A `Makefile` with matching target names delegates here, so `make seed` works
wherever make is available.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PY = str(VENV_PY if VENV_PY.exists() else Path(sys.executable))

# Stage at which each not-yet-built target becomes real. Empty now: `up` and
# `down` were the last two, and Stage 10 made them real.
DEFERRED: dict[str, tuple[int, str]] = {}
FRONTEND = ROOT / "frontend"
NPM = "npm.cmd" if os.name == "nt" else "npm"


def _run(*argv: str, cwd: Path = ROOT) -> int:
    print("+", " ".join(argv), flush=True)
    return subprocess.call(argv, cwd=str(cwd))


def _vars(args: list[str]) -> dict[str, str]:
    """Parse make-style ``KEY=value`` arguments."""
    out = {}
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            out[k.upper()] = v
    return out


# --- targets ---------------------------------------------------------------


def seed(args: list[str]) -> int:
    v = _vars(args)
    argv = [PY, "-m", "concordance.cli", "data", "seed"]
    argv += ["--corruption", v.get("CORRUPTION", "0.5")]
    argv += ["--providers", v.get("PROVIDERS", "50000")]
    argv += ["--sanctions", v.get("SANCTIONS", "5000")]
    if "SEED" in v:
        argv += ["--seed", v["SEED"]]
    return _run(*argv)


def verify(args: list[str]) -> int:
    return _run(PY, "-m", "concordance.cli", "data", "verify")


def inspect(args: list[str]) -> int:
    v = _vars(args)
    argv = [PY, "-m", "concordance.cli", "data", "inspect", "--limit", v.get("LIMIT", "10")]
    if "SCENARIO" in v:
        argv += ["--scenario", v["SCENARIO"]]
    return _run(*argv)


def blocking_recall(args: list[str]) -> int:
    v = _vars(args)
    argv = [PY, "-m", "concordance.cli", "match", "blocking-recall"]
    if "CORRUPTION" in v:
        argv += ["--corruption", v["CORRUPTION"]]
    if "MAX_CANDIDATES" in v:
        argv += ["--max-candidates", v["MAX_CANDIDATES"]]
    return _run(*argv)


def fit(args: list[str]) -> int:
    v = _vars(args)
    argv = [PY, "-m", "concordance.cli", "match", "fit"]
    if "CORRUPTION" in v:
        argv += ["--corruption", v["CORRUPTION"]]
    if "SEED" in v:
        argv += ["--seed", v["SEED"]]
    return _run(*argv)


def evaluate(args: list[str]) -> int:
    v = _vars(args)
    argv = [PY, "-m", "concordance.cli", "report", "eval"]
    argv += ["--strategy", v.get("STRATEGY", "probabilistic")]
    if "CORRUPTION" in v:
        argv += ["--corruption", v["CORRUPTION"]]
    if "SEED" in v:
        argv += ["--seed", v["SEED"]]
    return _run(*argv)


def sweep(args: list[str]) -> int:
    v = _vars(args)
    argv = [PY, "-m", "concordance.cli", "match", "sweep"]
    for key, flag in (
        ("LEVELS", "--levels"),
        ("STRATEGIES", "--strategies"),
        ("WORKERS", "--workers"),
        ("PROVIDERS", "--providers"),
        ("SANCTIONS", "--sanctions"),
        ("SEED", "--seed"),
    ):
        if key in v:
            argv += [flag, v[key]]
    return _run(*argv)


def api(args: list[str]) -> int:
    v = _vars(args)
    return _run(PY, "-m", "concordance.cli", "api", "serve", "--reload", "--port", v.get("PORT", "8000"))


def web(args: list[str]) -> int:
    return _run(NPM, "run", "dev", cwd=FRONTEND)


def client(args: list[str]) -> int:
    """Regenerate the web client's types from the API's OpenAPI schema."""
    return _run(NPM, "run", "api:schema", cwd=FRONTEND)


def web_build(args: list[str]) -> int:
    return _run(NPM, "run", "build", cwd=FRONTEND)


def e2e(args: list[str]) -> int:
    """The GATE 8 walkthrough in Chrome. Needs `make api`, `make worker` and `make web` running."""
    return _run(NPM, "run", "e2e", cwd=FRONTEND)


def migrate(args: list[str]) -> int:
    v = _vars(args)
    return _run(PY, "-m", "concordance.cli", "db", "upgrade", v.get("REVISION", "head"))


def load(args: list[str]) -> int:
    v = _vars(args)
    argv = [PY, "-m", "concordance.cli", "db", "load"]
    if "FROM" in v:
        argv += ["--from", v["FROM"]]
    return _run(*argv)


def worker(args: list[str]) -> int:
    v = _vars(args)
    argv = [PY, "-m", "concordance.cli", "jobs", "worker"]
    for key, flag in (("KINDS", "--kinds"), ("MAX_JOBS", "--max-jobs"), ("IDLE", "--idle-timeout")):
        if key in v:
            argv += [flag, v[key]]
    return _run(*argv)


def reconcile(args: list[str]) -> int:
    v = _vars(args)
    argv = [PY, "-m", "concordance.cli", "run", "reconcile"]
    argv += ["--strategy", v.get("STRATEGY", "probabilistic")]
    for key, flag in (("LIMIT", "--limit"), ("CONFIG", "--config"), ("SEED", "--seed")):
        if key in v:
            argv += [flag, v[key]]
    if v.get("QUEUE", "").lower() in {"1", "true", "yes"}:
        argv += ["--queue"]
    return _run(*argv)


def test(args: list[str]) -> int:
    return _run(PY, "-m", "pytest", *args)


def test_unit(args: list[str]) -> int:
    return _run(PY, "-m", "pytest", "-m", "unit", *args)


def cov(args: list[str]) -> int:
    return _run(PY, "-m", "pytest", "--cov=concordance", "--cov-report=term-missing")


def lint(args: list[str]) -> int:
    return _run(PY, "-m", "ruff", "check", "backend/src", "tests", "tasks.py")


def fmt(args: list[str]) -> int:
    rc = _run(PY, "-m", "ruff", "format", "backend/src", "tests", "tasks.py")
    return rc or _run(PY, "-m", "ruff", "check", "--fix", "backend/src", "tests", "tasks.py")


def typecheck(args: list[str]) -> int:
    return _run(PY, "-m", "mypy", cwd=BACKEND)


def up(args: list[str]) -> int:
    """Start api, worker and web together. See `scripts/supervise.py`."""
    v = _vars(args)
    argv = [PY, str(ROOT / "scripts" / "supervise.py")]
    detach = v.get("DETACH", "").lower() in {"1", "true", "yes"}
    argv += ["detached" if detach else "serve"]
    argv += ["--port", v.get("PORT", "8000"), "--web-port", v.get("WEB_PORT", "5173")]
    return _run(*argv)


def down(args: list[str]) -> int:
    return _run(PY, str(ROOT / "scripts" / "supervise.py"), "down")


def ps(args: list[str]) -> int:
    return _run(PY, str(ROOT / "scripts" / "supervise.py"), "status")


def logs(args: list[str]) -> int:
    """Tail a detached start's log. A foreground start already prints to its terminal."""
    log = ROOT / ".run" / "up.log"
    if not log.is_file():
        print(f"no {log} - `up` in the foreground prints to its own terminal")
        return 1
    # `errors="replace"` on the way out as well as in: the log holds whatever
    # Vite drew, and a console that cannot encode it should print a placeholder
    # rather than raise halfway through the tail.
    out = sys.stdout
    with contextlib.suppress(AttributeError, OSError, ValueError):
        out.reconfigure(encoding="utf-8", errors="replace")
    with log.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            print(line.rstrip())
    return 0


def preflight(args: list[str]) -> int:
    v = _vars(args)
    return _run(
        PY, "-m", "concordance.cli", "preflight",
        "--ports", f"api:{v.get('PORT', '8000')},web:{v.get('WEB_PORT', '5173')}",
        "--frontend", str(FRONTEND),
    )


def clean(args: list[str]) -> int:
    for pattern in ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"):
        for p in ROOT.rglob(pattern):
            if ".venv" not in p.parts:
                shutil.rmtree(p, ignore_errors=True)
    return 0


TARGETS: dict[str, Callable[[list[str]], int]] = {
    "seed": seed,
    "verify": verify,
    "inspect": inspect,
    "blocking-recall": blocking_recall,
    "fit": fit,
    "eval": evaluate,
    "sweep": sweep,
    "api": api,
    "web": web,
    "client": client,
    "web-build": web_build,
    "e2e": e2e,
    "migrate": migrate,
    "load": load,
    "reconcile": reconcile,
    "worker": worker,
    "test": test,
    "test-unit": test_unit,
    "cov": cov,
    "lint": lint,
    "fmt": fmt,
    "typecheck": typecheck,
    "clean": clean,
    "up": up,
    "down": down,
    "ps": ps,
    "logs": logs,
    "preflight": preflight,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        print("targets:", ", ".join(sorted(TARGETS)))
        print("deferred:", ", ".join(f"{k} (Stage {v[0]})" for k, v in sorted(DEFERRED.items())))
        return 0
    rc = 0
    args = [a for a in argv if "=" in a]
    names = [a for a in argv if "=" not in a]
    for name in names:
        if name in DEFERRED:
            stage, what = DEFERRED[name]
            print(f"{what} arrives at Stage {stage}. Not built yet - see docs/PLAN.md.")
            return 2
        fn = TARGETS.get(name)
        if fn is None:
            print(f"unknown target: {name}")
            return 2
        rc = fn(args)
        if rc != 0:
            return rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
