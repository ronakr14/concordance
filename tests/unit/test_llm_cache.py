"""The cache contract: keys separate what must be separate, entries survive."""

from __future__ import annotations

import json

import pytest

from concordance.llm.cache import FileCache, NullCache, cache_key

pytestmark = pytest.mark.unit


def test_key_is_stable_for_identical_input() -> None:
    a = cache_key("groq", "m", "v1", "hello")
    b = cache_key("groq", "m", "v1", "hello")
    assert a == b
    assert len(a) == 64


@pytest.mark.parametrize(
    "changed",
    [
        ("openrouter", "m", "v1", "hello"),
        ("groq", "other", "v1", "hello"),
        ("groq", "m", "v2", "hello"),
        ("groq", "m", "v1", "hello "),
    ],
)
def test_every_component_changes_the_key(changed: tuple[str, str, str, str]) -> None:
    assert cache_key(*changed) != cache_key("groq", "m", "v1", "hello")


def test_components_cannot_collide_across_the_split() -> None:
    """`("ab","c")` and `("a","bc")` are different inputs and must stay different keys."""
    assert cache_key("ab", "c", "v", "p") != cache_key("a", "bc", "v", "p")


def test_round_trip(tmp_path) -> None:
    cache = FileCache(tmp_path / "llm")
    key = cache_key("groq", "m", "v1", "prompt")
    assert cache.get(key) is None
    cache.put(key, {"key": key, "response": {"content": "{}"}})
    entry = cache.get(key)
    assert entry is not None
    assert entry["response"]["content"] == "{}"
    stats = cache.stats()
    assert stats == {"hits": 1, "misses": 1, "writes": 1, "entries": 1}


def test_entries_are_sharded_by_key_prefix(tmp_path) -> None:
    cache = FileCache(tmp_path / "llm")
    key = cache_key("groq", "m", "v1", "prompt")
    cache.put(key, {"key": key})
    assert cache.path_for(key).parent.name == key[:2]
    assert cache.path_for(key).exists()


def test_unreadable_entry_is_a_miss_not_a_crash(tmp_path) -> None:
    cache = FileCache(tmp_path / "llm")
    key = cache_key("groq", "m", "v1", "prompt")
    path = cache.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated", encoding="utf-8")
    assert cache.get(key) is None
    assert cache.stats()["misses"] == 1


def test_clear_removes_entries_and_empty_shards(tmp_path) -> None:
    cache = FileCache(tmp_path / "llm")
    for i in range(5):
        cache.put(cache_key("groq", "m", "v1", str(i)), {"i": i})
    assert cache.entry_count() == 5
    assert cache.clear() == 5
    assert cache.entry_count() == 0
    assert list(cache.root.iterdir()) == []


def test_entries_returns_every_stored_payload(tmp_path) -> None:
    cache = FileCache(tmp_path / "llm")
    for i in range(3):
        cache.put(
            cache_key("groq", "m", "v1", str(i)),
            {"provider": "groq", "model": "m", "response": {"total_tokens": i}},
        )
    entries = cache.entries()
    assert len(entries) == 3
    assert sum(e["response"]["total_tokens"] for e in entries) == 3


def test_entry_carries_everything_an_llm_calls_row_needs(tmp_path) -> None:
    """Stage 5's migration is an import, not a recomputation - so nothing may be missing."""
    cache = FileCache(tmp_path / "llm")
    key = cache_key("groq", "m", "v1", "p")
    cache.put(
        key,
        {
            "key": key,
            "provider": "groq",
            "model": "m",
            "prompt_version": "v1",
            "request": {"messages": []},
            "response": {
                "content": "{}",
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
                "latency_ms": 4,
                "cost_usd": 0.0,
            },
            "created_at": 1.0,
        },
    )
    stored = json.loads(cache.path_for(key).read_text(encoding="utf-8"))
    required = {"key", "provider", "model", "prompt_version", "request", "response", "created_at"}
    assert required <= set(stored)
    assert {"prompt_tokens", "completion_tokens", "latency_ms", "cost_usd"} <= set(
        stored["response"]
    )


def test_null_cache_never_stores() -> None:
    cache = NullCache()
    cache.put("k", {"a": 1})
    assert cache.get("k") is None
    assert cache.stats()["entries"] == 0
