"""The seam.

Everything the engine needs from storage and from blocking is expressed here as
a ``Protocol``. Stages 1-4 satisfy them with Parquet files and in-memory
indexes; Stage 5 satisfies them with Postgres. The engine never learns which.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from concordance.domain import Candidate, Provider, SanctionRecord


@runtime_checkable
class RecordStore(Protocol):
    """Read access to the provider master and the sanction records."""

    def all_providers(self) -> Iterable[Provider]:
        """Every provider, in a stable order."""
        ...

    def get_provider(self, pid: str) -> Provider | None:
        """One provider by id, or ``None`` if unknown."""
        ...

    def sanction_batch(self, offset: int, limit: int) -> list[SanctionRecord]:
        """A window of sanction records, in a stable order."""
        ...

    def provider_count(self) -> int:
        """Number of providers in the store."""
        ...

    def snapshot_hash(self) -> str:
        """Content hash of the store's current contents.

        Must be order-independent and stable across backends: the same data
        loaded into Postgres at Stage 5 has to produce the same hash it had as
        Parquet, or run replay cannot prove it replayed the same input.
        """
        ...


@runtime_checkable
class CandidateGenerator(Protocol):
    """Blocking: cheap reduction of 50k providers to a handful per record."""

    def build(self, store: RecordStore) -> None:
        """Index the store. Called once per dataset, before any lookup."""
        ...

    def candidates(self, rec: SanctionRecord) -> list[Candidate]:
        """Providers worth comparing against ``rec``."""
        ...


@runtime_checkable
class ResponseCache(Protocol):
    """Cache for LLM responses, keyed by model + prompt version + prompt."""

    def get(self, key: str) -> dict[str, Any] | None: ...

    def put(self, key: str, value: dict[str, Any]) -> None: ...

    def stats(self) -> dict[str, int]:
        """At least ``hits``, ``misses`` and ``entries``."""
        ...


@runtime_checkable
class StorageBackend(Protocol):
    """Blob storage. ``LocalStorage`` now, S3/Floci later if it is wanted."""

    def put(self, key: str, data: bytes) -> str:
        """Store ``data`` and return its URI."""
        ...

    def get(self, uri: str) -> bytes: ...

    def exists(self, uri: str) -> bool: ...

    def delete(self, uri: str) -> None: ...
