"""The credential scanner catches real keys and ignores documentation.

A scanner that reports nothing is indistinguishable from a scanner that is
broken, so both halves are asserted: fabricated keys of each shape must be
caught, and the placeholder strings that actually appear in this repository
must not be.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scan_credentials.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_credentials", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = _module()


def _hits(text: str) -> list[str]:
    found: list[str] = []
    scan._scan_text("x", text, found)
    return found


# Fabricated, with the right shape and no meaning. None of these is a key.
CAUGHT = [
    ("groq", "GROQ_API_KEY=gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2"),
    ("openrouter", "key: sk-or-v1-" + "0123456789abcdef0123456789abcdef"),
    ("aws", "AKIAQWERTYUIOPASDFGH"),
    ("private key", "-----BEGIN RSA PRIVATE KEY-----"),
    ("database url", "postgresql+psycopg://owner:Xk93mQ2vL0pR@db.example.com/app"),
    ("signing key", "JWT_SECRET=Zq8vN3mK1pR7tY2wB5xL9cD4"),
]


@pytest.mark.parametrize(("label", "text"), CAUGHT, ids=[c[0] for c in CAUGHT])
def test_a_credential_of_each_shape_is_caught(label: str, text: str) -> None:
    assert _hits(text), f"{label} was not caught"


# Every one of these is real text from this repository, and every one of them
# was reported as a leak before the placeholder list existed.
IGNORED = [
    ("env example", "JWT_SECRET=replace-with-a-long-random-string"),
    ("redaction docstring", '"""`postgresql://user:pw@host/db` -> `postgresql://user:***@host/db`."""'),
    ("redacted log line", "url='postgresql+psycopg://neondb_owner:***@ep-x.neon.tech/neondb'"),
    ("ci env file", "JWT_SECRET=ci-only-value-not-used-anywhere-else"),
    ("example placeholder", "GROQ_API_KEY=changeme"),
]


@pytest.mark.parametrize(("label", "text"), IGNORED, ids=[c[0] for c in IGNORED])
def test_documentation_is_not_reported_as_a_leak(label: str, text: str) -> None:
    assert not _hits(text), f"{label} was reported as a credential"


def test_the_scanner_skips_its_own_pattern_list() -> None:
    """Otherwise every run reports the examples it is made of."""
    assert scan._allowed("scripts/scan_credentials.py")
    assert scan._allowed("tests/unit/test_credential_scan.py")
    assert not scan._allowed("backend/src/concordance/config.py")
