"""The matching engine must not know how records are stored.

This is the test that keeps the Stage 0 seam honest. If someone reaches for a
DataFrame inside `matching/`, the Postgres implementation at Stage 5 stops
being a drop-in and the sweep stops being runnable without a database. Fail
here, loudly, rather than discover it three stages later.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "backend" / "src" / "concordance"

FORBIDDEN = {"pandas", "pyarrow", "sqlalchemy", "psycopg", "psycopg2", "alembic"}

# Modules that hold the storage-agnostic core. Everything else may import what
# it needs; these may not.
PURE = ["matching", "domain.py", "protocols.py"]


def _module_files() -> list[Path]:
    files: list[Path] = []
    for entry in PURE:
        target = PACKAGE / entry
        files.extend(sorted(target.rglob("*.py")) if target.is_dir() else [target])
    return [f for f in files if f.exists()]


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_modules(path: Path) -> set[str]:
    """Full dotted module names, not just their roots."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.unit
def test_pure_modules_have_no_storage_dependency() -> None:
    offenders: list[str] = []
    for path in _module_files():
        bad = _imported_roots(path) & FORBIDDEN
        if bad:
            offenders.append(f"{path.relative_to(PACKAGE)} imports {sorted(bad)}")
    assert not offenders, "storage dependency leaked into the engine core:\n" + "\n".join(offenders)


@pytest.mark.unit
def test_the_engine_does_not_import_the_llm_layer() -> None:
    """Stage 4 fills the grey-band seam; it must not become a dependency of it.

    `matching.adjudication` defines the protocol and `NullAdjudicator`; the
    concrete adjudicator lives in `llm/` and is injected. If that arrow ever
    reverses, `LLM_ENABLED=false` stops being a configuration and starts being
    a code path, and the sweep can no longer run without `httpx` installed.
    """
    offenders: list[str] = []
    for path in _module_files():
        leaked = sorted(m for m in _imported_modules(path) if m.startswith("concordance.llm"))
        if leaked:
            offenders.append(f"{path.relative_to(PACKAGE)} imports {leaked}")
    assert not offenders, "the LLM layer leaked into the engine core:\n" + "\n".join(offenders)


@pytest.mark.unit
def test_pure_modules_import_cleanly_without_optional_extras() -> None:
    """They must import with only the core dependencies present."""
    import concordance.domain
    import concordance.protocols

    assert concordance.domain.Provider is not None
    assert concordance.protocols.RecordStore is not None
