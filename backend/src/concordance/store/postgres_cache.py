"""`ResponseCache` backed by `llm_calls`. The second cache implementation.

The cache and the audit record are the same row. That is the point: a reviewer
asking "what did the model actually say about this record" and the router asking
"have I asked this before" are answered from one place, so the two can never
disagree about what the model said.

`FileCache` does not go away - it stays the cache the CLI and the sweep use,
because neither should require a database to run. `import_file_cache()` carries
its entries over so Stage 4's spend is not repeated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from concordance.db.models import LlmCall
from concordance.logging_setup import get_logger

log = get_logger("store.postgres_cache")


class PostgresCache:
    """Read-through cache over `llm_calls`, keyed by `cache_key`.

    Takes a `Session` and does not own the transaction. A cache write that is
    part of a reconciliation run belongs in that run's transaction: if the run
    rolls back, the call it never used should not be left behind claiming it
    happened.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.session.scalar(select(LlmCall).where(LlmCall.cache_key == key))
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return {
            "key": row.cache_key,
            "provider": row.provider,
            "model": row.model,
            "prompt_version": row.prompt_version,
            "request": dict(row.request or {}),
            "response": dict(row.response or {}),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def put(self, key: str, value: dict[str, Any]) -> None:
        """Insert, or leave the existing row alone.

        `ON CONFLICT DO NOTHING` rather than an upsert: two workers adjudicating
        the same pair concurrently produce answers that are equivalent by
        construction (same key means same provider, model, prompt version and
        prompt), so the second writer has nothing to correct and overwriting
        would only churn the row a `match_results.llm_call_id` points at.
        """
        # A `FileCache` entry carries latency, tokens and cost inside its
        # `response` block (that is the serialized `LLMResponse`); a caller
        # building a row by hand may pass them at the top level. Read both.
        response = value.get("response") or {}
        usage = response.get("usage") or {}

        def field(name: str) -> Any:
            return value.get(name) or response.get(name) or usage.get(name) or 0

        stmt = (
            insert(LlmCall)
            .values(
                cache_key=key,
                provider=value.get("provider") or response.get("provider") or "",
                model=value.get("model") or response.get("model") or "",
                prompt_version=value.get("prompt_version", ""),
                request=value.get("request") or {},
                response=response,
                latency_ms=int(field("latency_ms")),
                prompt_tokens=int(field("prompt_tokens")),
                completion_tokens=int(field("completion_tokens")),
                cost_usd=field("cost_usd"),
            )
            .on_conflict_do_nothing(index_elements=[LlmCall.cache_key])
        )
        self.session.execute(stmt)
        self.writes += 1

    def stats(self) -> dict[str, int]:
        entries = int(self.session.scalar(select(func.count()).select_from(LlmCall)) or 0)
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "entries": entries,
        }


def import_file_cache(session: Session, cache_dir: Path | str) -> dict[str, int]:
    """Copy every `FileCache` entry into `llm_calls`.

    Stage 4 paid for these answers. Migrating them means the first Postgres run
    starts warm, and - more usefully - that the cache keys stay stable across
    the move, so a record adjudicated before the migration is not adjudicated
    again after it.

    Returns counts rather than logging only, because the caller is a CLI command
    that should print what it did.
    """
    import json

    root = Path(cache_dir)
    imported = skipped = failed = 0
    cache = PostgresCache(session)
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed += 1
            continue
        key = payload.get("key") or path.stem
        if session.scalar(select(LlmCall.id).where(LlmCall.cache_key == key)) is not None:
            skipped += 1
            continue
        cache.put(key, payload)
        imported += 1
    log.info("llm_cache.import", imported=imported, skipped=skipped, failed=failed)
    return {"imported": imported, "skipped": skipped, "failed": failed}


__all__ = ["PostgresCache", "import_file_cache"]
