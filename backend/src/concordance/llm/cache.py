"""The response cache. One JSON file per call, keyed by content.

Two jobs, and the second is the one that shapes the design.

**Determinism.** A reconciliation run that consults a model is not reproducible
unless the model's answers are. With a read-through cache keyed by the exact
bytes that were sent, replaying a run re-derives every decision without a single
network call - which is what makes `concordance replay` at Stage 6 an assertion
rather than a hope. It is also why the key includes the prompt version: editing
a prompt must invalidate every answer it produced, silently reusing them would
make a run that claims one prompt version and ran on another.

**Migration.** Each entry stores everything the Stage 5 `llm_calls` table needs -
provider, model, prompt version, the request, the response, latency, tokens,
cost. Stage 5's migration reads this directory and inserts rows; nothing has to
be recomputed or re-requested, so the development cache keeps its value instead
of being thrown away at the persistence boundary.

Failures are never cached. A rate limit is not an answer, and caching one would
turn a transient outage into a permanently wrong result.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from concordance.logging_setup import get_logger

log = get_logger("llm.cache")

#: Characters of the key used as a subdirectory name. Two hex characters give
#: 256 shards, which keeps directory listings small on Windows without nesting
#: deeply enough to annoy anyone reading the tree by hand.
SHARD_LEN = 2


def cache_key(provider: str, model: str, prompt_version: str, rendered_prompt: str) -> str:
    """`sha256(provider + model + prompt_version + rendered_prompt)`.

    Joined with a separator that cannot occur in any component, so two
    different splits cannot collide into one key - ``("ab", "c")`` and
    ``("a", "bc")`` are distinct inputs and must stay distinct keys.
    """
    joined = "\x1f".join((provider, model, prompt_version, rendered_prompt))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}


@dataclass
class FileCache:
    """`ResponseCache` on the filesystem. `PostgresCache` replaces it at Stage 5.

    Stays in the project after Stage 5 regardless: the CLI and the evaluation
    sweep run without a database, and they still need the cache.
    """

    root: Path
    counters: CacheStats = field(default_factory=CacheStats)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- layout -----------------------------------------------------------
    def path_for(self, key: str) -> Path:
        return self.root / key[:SHARD_LEN] / f"{key}.json"

    # -- protocol ---------------------------------------------------------
    def get(self, key: str) -> dict[str, Any] | None:
        path = self.path_for(key)
        if not path.exists():
            self.counters.misses += 1
            return None
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A truncated entry - a process killed mid-write on a filesystem
            # without atomic rename. Treat it as absent and let it be rewritten
            # rather than failing a run over a cache file.
            log.warning("llm.cache.unreadable", key=key, path=str(path))
            self.counters.misses += 1
            return None
        self.counters.hits += 1
        return payload

    def put(self, key: str, value: dict[str, Any]) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp file and rename: a reader never sees a
        # half-written entry, and two workers racing on the same key both end
        # up with one complete file rather than an interleaved one.
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.counters.writes += 1

    def stats(self) -> dict[str, int]:
        """`hits`, `misses`, `writes` and the on-disk entry count."""
        return {**self.counters.as_dict(), "entries": self.entry_count()}

    # -- maintenance ------------------------------------------------------
    def entry_count(self) -> int:
        return sum(1 for _ in self.root.glob("*/*.json"))

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.glob("*/*.json"))

    def entries(self) -> list[dict[str, Any]]:
        """Every entry, for the Stage 5 migration and for `llm cache-stats`."""
        out: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*/*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.warning("llm.cache.unreadable", path=str(path))
        return out

    def clear(self) -> int:
        """Delete every entry. Returns how many were removed."""
        removed = 0
        for path in list(self.root.glob("*/*.json")):
            path.unlink(missing_ok=True)
            removed += 1
        for shard in sorted(self.root.iterdir()):
            if shard.is_dir() and not any(shard.iterdir()):
                shard.rmdir()
        self.counters = CacheStats()
        log.info("llm.cache.cleared", removed=removed, root=str(self.root))
        return removed


@dataclass
class NullCache:
    """Caches nothing. For tests that are measuring call counts, not caching."""

    counters: CacheStats = field(default_factory=CacheStats)

    def get(self, key: str) -> dict[str, Any] | None:
        self.counters.misses += 1
        return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        return None

    def stats(self) -> dict[str, int]:
        return {**self.counters.as_dict(), "entries": 0}


__all__ = ["SHARD_LEN", "CacheStats", "FileCache", "NullCache", "cache_key"]
