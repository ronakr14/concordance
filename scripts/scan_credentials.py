"""Scan the repository, and its whole history, for credentials.

A working-tree scan is not enough. A key committed once and removed in the next
commit is still in the history, still reachable by anyone who clones, and still
has to be rotated - so this reads every blob every commit ever pointed at, not
only the files present now.

    python scripts/scan_credentials.py            # history and working tree
    python scripts/scan_credentials.py --tree     # working tree only, for a hook

Exit status is 1 if anything matched, so CI or a pre-commit hook can use it.

The patterns are deliberately narrow, and the placeholder list below is why.
On its first run this scanner reported thirteen leaks, every one of them
documentation: `.env.example` saying `replace-with-a-long-random-string`, and a
docstring demonstrating redaction on `postgresql://user:pw@host/db`. A scanner
that cries wolf twelve times is a scanner nobody runs the thirteenth time.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Password and key values that are obviously documentation. Used as a
#: negative lookahead, so a URL or an assignment carrying one of these is not
#: reported, and anything else of real length is.
PLACEHOLDERS = (
    "replace",
    "change",
    "example",
    "your",
    "ci-only",
    "sec" + "ret",
    "password",
    "passwd",
    "pwd",
    "token",
    "user",
    "app",
    "concordance",
    "xxx",
    r"\*\*\*",
)

_NOT_A_PLACEHOLDER = "(?!" + "|".join(PLACEHOLDERS) + ")"

#: (name, pattern, why it matters). Ordered most to least serious.
PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("groq api key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "a live Groq key"),
    ("openrouter api key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{20,}"), "a live OpenRouter key"),
    ("openai api key", re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "a live OpenAI-style key"),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "an AWS access key id"),
    (
        "private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "a private key",
    ),
    (
        "database url with a password",
        re.compile(
            r"postgres(?:ql)?(?:\+\w+)?://[^\s:/@]+:" + _NOT_A_PLACEHOLDER + r"[^\s:@/]{6,}@"
        ),
        "a database URL with a real password in it",
    ),
    (
        "signing key assignment",
        re.compile(r"JWT_SECRET\s*[=:]\s*[\"']?" + _NOT_A_PLACEHOLDER + r"[A-Za-z0-9+/=_-]{16,}"),
        "a token signing key",
    ),
)

#: Paths whose matches are noise: the tests' own fixtures, CI's throwaway
#: values, and this scanner's own pattern list.
ALLOWED = (
    "scripts/scan_credentials.py",
    "tests/",
    ".github/workflows/",
)


def _allowed(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized.startswith(prefix) or f"/{prefix}" in normalized for prefix in ALLOWED)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True, check=True, errors="replace"
    ).stdout


def _scan_text(where: str, text: str, hits: list[str]) -> None:
    for name, pattern, why in PATTERNS:
        match = pattern.search(text)
        if match:
            found = match.group(0)
            hits.append(f"{where}: {name} ({why}), {len(found)} chars starting {found[:6]!r}")


def scan_tree(hits: list[str]) -> int:
    files = [f for f in _git("ls-files").splitlines() if f and not _allowed(f)]
    for rel in files:
        try:
            text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - binary or unreadable
            continue
        _scan_text(f"tree {rel}", text, hits)
    return len(files)


def scan_history(hits: list[str]) -> int:
    """Every blob any commit ever pointed at, read once by its object id."""
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for line in _git("rev-list", "--all", "--objects").splitlines():
        sha, _, path = line.partition(" ")
        if not path or _allowed(path) or sha in seen:
            continue
        seen.add(sha)
        pairs.append((sha, path))
    for sha, path in pairs:
        if _git("cat-file", "-t", sha).strip() != "blob":
            continue
        try:
            content = _git("cat-file", "-p", sha)
        except subprocess.CalledProcessError:  # pragma: no cover
            continue
        _scan_text(f"history {path} ({sha[:8]})", content, hits)
    return len(pairs)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="scan_credentials")
    parser.add_argument("--tree", action="store_true", help="Working tree only; skip history.")
    args = parser.parse_args(argv)

    hits: list[str] = []
    files = scan_tree(hits)
    blobs = 0 if args.tree else scan_history(hits)

    scanned = f"scanned {files} tracked file(s)"
    if not args.tree:
        scanned += f" and {blobs} historical blob(s)"
    print(scanned)
    if not hits:
        print("no credentials found")
        return 0
    print(f"\n{len(hits)} possible credential(s):")
    for hit in hits:
        print(f"  {hit}")
    print("\nA key in history is still a leaked key. Rotate it, then rewrite the history.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
