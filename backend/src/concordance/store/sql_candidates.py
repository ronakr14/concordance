"""`CandidateGenerator` over Postgres. The second blocking implementation.

The design constraint that shapes this file: GATE 5 requires that
`SqlCandidateGenerator` and `InMemoryCandidateGenerator` return **identical**
candidate sets, and that nothing in `matching/` changes to make it happen. So
this module does not re-express the blocking rules in SQL. It calls the same
`_keys()` the in-memory generator indexes with, and looks the resulting keys up
in `provider_block_keys` - one indexed query for every exact block, as the
checklist asks, but the keys come from the engine rather than from a second
implementation that would drift the first time a block changes.

Reaching for a private method is deliberate and is the smaller of two evils:
promoting `_keys` to a module-level function would be a change to `matching/`,
and the gate exists precisely to catch storage work leaking into the engine. The
adapter below is the seam absorbing the awkwardness, which is its job.

**Where the two generators used to differ.** The in-memory trigram index could
skip any trigram whose posting list exceeded `max_posting` on the grounds that a
trigram that common contributes noise rather than signal. Postgres' `pg_trgm`
has no equivalent rule, so with that guard at 2,000 the two sides disagreed on
85 of 300 records at full dataset size - and the equivalence test passed only
because its fixture sat below the threshold, which tested the fixture rather
than the claim. The guard now defaults to off: measured across 120 records at
full size the two generators return identical candidate sets in identical
order. Turning it back on reintroduces the divergence by design, so nothing on
a compared path may set it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Float, String, bindparam, func, select, text, tuple_
from sqlalchemy.orm import Session

from concordance.db.models import Provider as ProviderRow
from concordance.db.models import ProviderBlockKey
from concordance.domain import Candidate, SanctionRecord
from concordance.logging_setup import get_logger
from concordance.matching.blocking import (
    BLOCK_TRIGRAM,
    InMemoryCandidateGenerator,
)
from concordance.matching.normalization import NormalizedRecord, normalize_sanction

log = get_logger("store.sql_candidates")

#: One throwaway instance, used only for its key derivation. It indexes nothing
#: and holds no providers; `_keys` reads the record it is handed and nothing
#: else.
_KEY_SOURCE = InMemoryCandidateGenerator()


def block_keys(record: NormalizedRecord, *, indexing: bool) -> list[tuple[str, str]]:
    """Every `(block, key)` pair this record participates in.

    The one derivation of blocking keys in the system. The loader calls it with
    `indexing=True` to populate `provider_block_keys`; the generator calls it
    with `indexing=False` to look them up, which is what adds the adjacent
    birth-year keys on the query side only.
    """
    return _KEY_SOURCE._keys(record, indexing=indexing)


def trigram_value(record: NormalizedRecord) -> str:
    """The exact string the trigram block compares - spaces removed.

    `pg_trgm` splits on non-alphanumerics and pads each word separately, so a
    value containing a space would produce a different trigram set in Postgres
    than the in-memory index produces for the same name. Removing the spaces on
    both sides removes the discrepancy rather than papering over it.
    """
    value = record.org_name_norm if record.is_organization else record.name_sorted_norm
    return value.replace(" ", "")


@dataclass
class SqlBlockingStats:
    """What blocking cost, reported the way the in-memory generator reports it."""

    lookups: int = 0
    exact_queries: int = 0
    trigram_queries: int = 0
    candidates_returned: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "lookups": self.lookups,
            "exact_queries": self.exact_queries,
            "trigram_queries": self.trigram_queries,
            "candidates_returned": self.candidates_returned,
        }


@dataclass
class SqlCandidateGenerator:
    """Blocking as two indexed queries: exact keys, then trigram similarity."""

    session: Session
    max_candidates: int = 50
    trigram_floor: float = 0.3
    stats: SqlBlockingStats = field(default_factory=SqlBlockingStats)

    # -- CandidateGenerator ----------------------------------------------
    def build(self, store: Any) -> None:  # noqa: ARG002 - the protocol's signature
        """No-op: the index is `provider_block_keys`, written by the loader.

        Declared because the protocol declares it. Rebuilding an index that is
        already a table on every run would be the opposite of what moving to a
        database bought.
        """
        return None

    def candidates(self, rec: SanctionRecord) -> list[Candidate]:
        return self.candidates_for(normalize_sanction(rec))

    def candidates_for(self, record: NormalizedRecord) -> list[Candidate]:
        """Same contract as the in-memory generator, same ordering rule."""
        self.stats.lookups += 1
        hits: dict[str, list[str]] = {}
        ordinals: dict[str, int] = {}

        def note(provider_id: str, block: str, ordinal: int) -> None:
            blocks = hits.setdefault(provider_id, [])
            if block not in blocks:
                blocks.append(block)
            ordinals[provider_id] = ordinal

        pairs = [(b, k) for b, k in block_keys(record, indexing=False)]
        if pairs:
            self.stats.exact_queries += 1
            stmt = select(
                ProviderBlockKey.provider_id,
                ProviderBlockKey.block,
                ProviderBlockKey.ordinal,
            ).where(
                tuple_(ProviderBlockKey.block, ProviderBlockKey.key).in_(
                    bindparam("pairs", value=pairs, expanding=True)
                )
            )
            for provider_id, block, ordinal in self.session.execute(stmt):
                note(provider_id, block, ordinal)

        # The fuzzy block runs last and only fills the space the exact blocks
        # left - the same ordering the in-memory generator uses, for the same
        # reason: it is the expensive one and the one that over-returns.
        remaining = max(0, self.max_candidates - len(hits))
        value = trigram_value(record)
        if remaining and value:
            self.stats.trigram_queries += 1
            similarity = func.similarity(ProviderRow.trigram_key, bindparam("value", value, String))
            stmt = (
                select(ProviderRow.provider_id, ProviderRow.ordinal, similarity.label("sim"))
                .where(similarity >= bindparam("floor", self.trigram_floor, Float))
                .order_by(text("sim DESC"), ProviderRow.ordinal)
                .limit(remaining * 2)
            )
            for provider_id, ordinal, _sim in self.session.execute(stmt):
                note(provider_id, BLOCK_TRIGRAM, ordinal)

        ranked = sorted(hits.items(), key=lambda item: (-len(item[1]), ordinals[item[0]]))
        out = [
            Candidate(provider_id=pid, blocking_keys=tuple(blocks))
            for pid, blocks in ranked[: self.max_candidates]
        ]
        self.stats.candidates_returned += len(out)
        return out

    # -- batched path ------------------------------------------------------
    def candidates_batch(
        self, records: Sequence[NormalizedRecord]
    ) -> list[list[Candidate]]:
        """`candidates_for` over many records, in a fixed number of queries.

        Per-record blocking is two round trips each, and the scorer then asks
        for one normalized provider per candidate - roughly forty-seven round
        trips per sanction record. Against a local socket that is merely
        wasteful; against a database across a network it is the difference
        between a run that finishes and one that does not. This method answers
        a whole chunk with one exact-block query and one trigram query, and
        `normalized_providers` below answers the scorer's side the same way.

        The result is positional: element *i* is the candidate list for
        ``records[i]``, identical to what `candidates_for` would return for it.
        """
        if not records:
            return []

        self.stats.lookups += len(records)
        hits: list[dict[str, list[str]]] = [{} for _ in records]
        ordinals: list[dict[str, int]] = [{} for _ in records]

        def note(slot: int, provider_id: str, block: str, ordinal: int) -> None:
            blocks = hits[slot].setdefault(provider_id, [])
            if block not in blocks:
                blocks.append(block)
            ordinals[slot][provider_id] = ordinal

        # -- exact blocks, one query for the chunk -------------------------
        # The pair list is deduplicated across records: two sanction records
        # sharing a last name should not send that key twice. `slots_by_pair`
        # fans the single result row back out to every record that asked.
        slots_by_pair: dict[tuple[str, str], list[int]] = {}
        for slot, record in enumerate(records):
            for pair in block_keys(record, indexing=False):
                slots_by_pair.setdefault(pair, []).append(slot)

        if slots_by_pair:
            self.stats.exact_queries += 1
            stmt = select(
                ProviderBlockKey.block,
                ProviderBlockKey.key,
                ProviderBlockKey.provider_id,
                ProviderBlockKey.ordinal,
            ).where(
                tuple_(ProviderBlockKey.block, ProviderBlockKey.key).in_(
                    bindparam("pairs", value=list(slots_by_pair), expanding=True)
                )
            )
            for block, key, provider_id, ordinal in self.session.execute(stmt):
                for slot in slots_by_pair.get((block, key), ()):
                    note(slot, provider_id, block, ordinal)

        # -- trigram block, one query for the chunk ------------------------
        # Each record's fuzzy budget depends on how many candidates its exact
        # blocks already found, so the per-record limit travels with the row
        # and a LATERAL join applies it. `%` is what the GIN index answers;
        # the explicit `similarity() >=` beside it keeps the boundary case
        # identical to the in-memory floor rather than trusting the operator
        # and the threshold GUC to agree on `>` versus `>=`.
        wanted: list[tuple[int, str, int]] = []
        for slot, record in enumerate(records):
            remaining = max(0, self.max_candidates - len(hits[slot]))
            value = trigram_value(record)
            if remaining and value:
                wanted.append((slot, value, remaining * 2))

        if wanted:
            self.stats.trigram_queries += 1
            # `SET LOCAL` takes no bind parameters; `set_config` is the same
            # knob as a function, and its third argument scopes the change to
            # this transaction rather than the pooled connection.
            self.session.execute(
                text("SELECT set_config('pg_trgm.similarity_threshold', CAST(:floor AS text), true)"),
                {"floor": self.trigram_floor},
            )
            values = ", ".join(
                f"(:slot_{i}, :val_{i}, :lim_{i})" for i in range(len(wanted))
            )
            params: dict[str, Any] = {"floor": self.trigram_floor}
            for i, (slot, value, limit) in enumerate(wanted):
                params[f"slot_{i}"] = slot
                params[f"val_{i}"] = value
                params[f"lim_{i}"] = limit
            sql = text(
                f"""
                SELECT v.slot, c.provider_id, c.ordinal
                FROM (VALUES {values}) AS v(slot, val, lim)
                CROSS JOIN LATERAL (
                    SELECT p.provider_id,
                           p.ordinal,
                           similarity(p.trigram_key, v.val) AS sim
                    FROM providers p
                    WHERE p.trigram_key % v.val
                      AND similarity(p.trigram_key, v.val) >= :floor
                    ORDER BY sim DESC, p.ordinal
                    LIMIT v.lim
                ) AS c
                """
            )
            for slot, provider_id, ordinal in self.session.execute(sql, params):
                note(int(slot), provider_id, BLOCK_TRIGRAM, ordinal)

        out: list[list[Candidate]] = []
        for slot in range(len(records)):
            ranked = sorted(
                hits[slot].items(), key=lambda item: (-len(item[1]), ordinals[slot][item[0]])
            )
            picked = [
                Candidate(provider_id=pid, blocking_keys=tuple(blocks))
                for pid, blocks in ranked[: self.max_candidates]
            ]
            self.stats.candidates_returned += len(picked)
            out.append(picked)
        return out

    def normalized_providers(self, provider_ids: Iterable[str]) -> dict[str, NormalizedRecord]:
        """`normalized_provider` for many ids, in one query.

        The scorer asks for the normalized form of every candidate it was
        handed. One query per candidate is the single largest source of round
        trips in a run; one query per chunk is the same answer.
        """
        from concordance.matching.normalization import normalize_provider
        from concordance.store.postgres_store import _to_provider

        ids = list(dict.fromkeys(provider_ids))
        if not ids:
            return {}
        rows = self.session.scalars(
            select(ProviderRow).where(ProviderRow.provider_id.in_(ids))
        )
        return {row.provider_id: normalize_provider(_to_provider(row)) for row in rows}

    # -- introspection ----------------------------------------------------
    def normalized_provider(self, provider_id: str) -> NormalizedRecord | None:
        """The normalized form of one provider, for the comparator stage.

        Rebuilt from the stored columns rather than cached in memory: a process
        that holds 50,000 normalized providers to score one file has not moved
        to a database, it has moved its RAM problem behind a connection.
        """
        from concordance.matching.normalization import normalize_provider
        from concordance.store.postgres_store import _to_provider

        row = self.session.scalar(select(ProviderRow).where(ProviderRow.provider_id == provider_id))
        return None if row is None else normalize_provider(_to_provider(row))


__all__ = ["SqlBlockingStats", "SqlCandidateGenerator", "block_keys", "trigram_value"]
