"""Blocking: 250 million pairs down to a few dozen per record.

50k providers x 5k records is 250M comparisons. The union of a handful of cheap
exact-lookup blocks plus one fuzzy block reaches the same true pairs at a
fraction of the cost. Blocking recall is the ceiling on system recall - a pair
that never becomes a candidate cannot be scored, no matter how good the model
is - so every block records *which* block produced each candidate, and
`eval/blocking_recall.py` reports what was missed and why.

This is the in-memory implementation of the Stage 0 `CandidateGenerator`
protocol. The SQL one arrives at Stage 5; this one is kept permanently, because
it is what makes the Stage 3 corruption sweep fast enough to run repeatedly.

The trigram index is in-house on purpose: `pg_trgm` is not available before
Stage 5, and an engine whose fuzzy block only works with a database attached
cannot be swept locally.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from concordance.domain import Candidate, Provider, SanctionRecord
from concordance.matching.normalization import (
    NormalizedRecord,
    normalize_provider,
    normalize_sanction,
)
from concordance.protocols import RecordStore

# Block names, also used as the keys of the per-block contribution report.
BLOCK_NPI = "npi"
BLOCK_STATE_DOB = "state_dob"
BLOCK_PHONETIC_STATE = "phonetic_state"
BLOCK_ZIP_NAME3 = "zip_name3"
BLOCK_LICENSE = "license"
BLOCK_TRIGRAM = "trigram"
BLOCK_EIN = "ein"
BLOCK_ORG_TOKEN_STATE = "org_token_state"
BLOCK_ORG_ACRONYM = "org_acronym"

INDIVIDUAL_BLOCKS = (
    BLOCK_NPI,
    BLOCK_STATE_DOB,
    BLOCK_PHONETIC_STATE,
    BLOCK_ZIP_NAME3,
    BLOCK_LICENSE,
    BLOCK_TRIGRAM,
)
ORGANIZATION_BLOCKS = (BLOCK_NPI, BLOCK_EIN, BLOCK_ORG_TOKEN_STATE, BLOCK_ORG_ACRONYM, BLOCK_TRIGRAM)
ALL_BLOCKS = tuple(dict.fromkeys(INDIVIDUAL_BLOCKS + ORGANIZATION_BLOCKS))


def trigrams(value: str) -> frozenset[str]:
    """Character trigrams over a padded string, as `pg_trgm` forms them.

    Padding means short strings still produce keys, and a shared prefix counts:
    `SMITH` and `SMITHE` share four of their six trigrams.
    """
    cleaned = value.replace(" ", "")
    if not cleaned:
        return frozenset()
    padded = f"  {cleaned} "
    return frozenset(padded[i : i + 3] for i in range(len(padded) - 2))


@dataclass
class TrigramIndex:
    """Inverted trigram index with a Jaccard-style overlap floor.

    Rare trigrams are the useful ones, so a posting list longer than
    ``max_posting`` can be skipped at query time: `MAR`, `SON` and `INC` would
    otherwise return a large share of the file and swamp the cap.

    **The guard defaults to off, and only off preserves parity with Postgres.**
    `pg_trgm` has no equivalent cut-off, so an index that skips long postings
    returns a strictly smaller candidate set than `SqlCandidateGenerator` does
    for the same record - measured at 85 of 300 records disagreeing with the
    guard at 2,000, and 0 of 120 with it off. Two implementations of one
    protocol that disagree are worth less than the time the guard saves, which
    on 5,000 records is roughly 25 seconds. Set it only where a divergent but
    faster index is knowingly acceptable, never on the path a gate compares.
    """

    floor: float = 0.3
    max_posting: int | None = None
    postings: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    sizes: list[int] = field(default_factory=list)

    def add(self, doc_id: int, value: str) -> None:
        grams = trigrams(value)
        self.sizes.append(len(grams))
        for gram in grams:
            self.postings[gram].append(doc_id)

    def query(self, value: str, limit: int) -> list[tuple[int, float]]:
        """Document ids whose trigram overlap with ``value`` clears the floor."""
        grams = trigrams(value)
        if not grams:
            return []
        overlap: dict[int, int] = defaultdict(int)
        for gram in grams:
            posting = self.postings.get(gram)
            if not posting or (self.max_posting is not None and len(posting) > self.max_posting):
                continue
            for doc_id in posting:
                overlap[doc_id] += 1

        scored: list[tuple[int, float]] = []
        for doc_id, shared in overlap.items():
            union = len(grams) + self.sizes[doc_id] - shared
            similarity = shared / union if union else 0.0
            if similarity >= self.floor:
                scored.append((doc_id, similarity))
        # Deterministic: similarity descending, then document order.
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]


@dataclass
class BlockingStats:
    """What the index cost to build - reported, not guessed."""

    providers: int = 0
    individuals: int = 0
    organizations: int = 0
    keys_by_block: dict[str, int] = field(default_factory=dict)
    trigram_postings: int = 0
    build_seconds: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "providers": self.providers,
            "individuals": self.individuals,
            "organizations": self.organizations,
            "keys_by_block": dict(self.keys_by_block),
            "trigram_postings": self.trigram_postings,
            "build_seconds": round(self.build_seconds, 3),
        }


class InMemoryCandidateGenerator:
    """Union of exact blocks plus one trigram block, built once per dataset."""

    def __init__(self, max_candidates: int = 50, trigram_floor: float = 0.3) -> None:
        self.max_candidates = max_candidates
        self.trigram_floor = trigram_floor
        self._indexes: dict[str, dict[str, list[int]]] = {b: defaultdict(list) for b in ALL_BLOCKS}
        self._trigram = TrigramIndex(floor=trigram_floor)
        self._provider_ids: list[str] = []
        self._normalized: list[NormalizedRecord] = []
        self._doc_of: dict[str, int] = {}
        self.stats = BlockingStats()
        self._built = False

    # -- building ---------------------------------------------------------
    def build(self, store: RecordStore) -> None:
        """Index every provider in ``store``. Idempotent per instance."""
        self.build_from(store.all_providers())

    def build_from(self, providers: Iterable[Provider]) -> None:
        """Index an iterable of providers directly - the testable entry point."""
        started = time.perf_counter()
        for provider in providers:
            self.add(normalize_provider(provider))
        self.stats.providers = len(self._provider_ids)
        self.stats.keys_by_block = {b: len(self._indexes[b]) for b in ALL_BLOCKS}
        self.stats.trigram_postings = sum(len(v) for v in self._trigram.postings.values())
        self.stats.build_seconds = time.perf_counter() - started
        self._built = True

    def add(self, record: NormalizedRecord) -> None:
        """Index one already-normalized provider."""
        doc_id = len(self._provider_ids)
        self._provider_ids.append(record.record_id)
        self._doc_of[record.record_id] = doc_id
        self._normalized.append(record)
        if record.is_organization:
            self.stats.organizations += 1
        else:
            self.stats.individuals += 1

        for block, key in self._keys(record, indexing=True):
            self._indexes[block][key].append(doc_id)
        self._trigram.add(doc_id, self._trigram_value(record))

    # -- keys -------------------------------------------------------------
    @staticmethod
    def _trigram_value(record: NormalizedRecord) -> str:
        return record.org_name_norm if record.is_organization else record.name_sorted_norm

    def _keys(self, record: NormalizedRecord, indexing: bool) -> list[tuple[str, str]]:
        """Every (block, key) pair this record participates in.

        The same function serves indexing and lookup, which is the only way to
        be sure a provider is findable by the key a record will ask for.
        """
        keys: list[tuple[str, str]] = []
        address = record.address

        if record.has_valid_npi:
            keys.append((BLOCK_NPI, record.npi))

        if record.license_number and record.license_state:
            keys.append((BLOCK_LICENSE, f"{record.license_number}|{record.license_state}"))

        if record.is_organization:
            if record.ein:
                keys.append((BLOCK_EIN, record.ein))
            state = address.state
            if state:
                for token in record.org_tokens[:4]:
                    keys.append((BLOCK_ORG_TOKEN_STATE, f"{token}|{state}"))
            for acronym in (record.org_acronym_key, record.dba_acronym_key):
                if acronym:
                    keys.append((BLOCK_ORG_ACRONYM, acronym))
            # A name that is already an acronym has no acronym of its own, so it
            # is indexed under itself - that is what lets "RFPG" meet "Riverside
            # Family Practice Group".
            for name in (record.org_name_norm, record.dba_name_norm):
                parts = name.split(" ")
                if len(parts) == 1 and 2 <= len(parts[0]) <= 6 and parts[0].isalpha():
                    keys.append((BLOCK_ORG_ACRONYM, parts[0]))
            # A record filed as an organization may still match a person, and
            # the reverse; the DBA tokens cover the acronym-vs-expanded case.
            for token in record.org_tokens[:2]:
                if len(token) > 2 and address.zip5:
                    keys.append((BLOCK_ZIP_NAME3, f"{address.zip5}|{token[:3]}"))
            return keys

        dob = record.dob
        state = address.state
        if state and dob.year:
            # The key is the year alone, so an off-by-one day or a month/day
            # swap still blocks. A query also asks the adjacent years, which is
            # what carries the off-by-one-year corruption.
            years = (dob.year,) if indexing else (dob.year, dob.year - 1, dob.year + 1)
            keys.extend((BLOCK_STATE_DOB, f"{state}|{year}") for year in years)

        if state:
            for key in record.phonetic_keys:
                keys.append((BLOCK_PHONETIC_STATE, f"{key}|{state}"))

        if address.zip5 and record.last_norm:
            keys.append((BLOCK_ZIP_NAME3, f"{address.zip5}|{record.last_norm[:3]}"))
        if address.zip5 and record.first_norm:
            keys.append((BLOCK_ZIP_NAME3, f"{address.zip5}|{record.first_norm[:3]}"))

        return keys

    # -- querying ---------------------------------------------------------
    def candidates(self, rec: SanctionRecord) -> list[Candidate]:
        """Providers worth comparing against ``rec``, best-blocked first."""
        return self.candidates_for(normalize_sanction(rec))

    def candidates_for(self, record: NormalizedRecord) -> list[Candidate]:
        """Same, for an already-normalized record."""
        if not self._built and not self._provider_ids:
            raise RuntimeError("build() the index before asking for candidates")

        hits: dict[int, list[str]] = {}

        def note(doc_id: int, block: str) -> None:
            blocks = hits.setdefault(doc_id, [])
            if block not in blocks:
                blocks.append(block)

        for block, key in self._keys(record, indexing=False):
            for doc_id in self._indexes[block].get(key, ()):
                note(doc_id, block)

        # The fuzzy block runs last and only fills the space the exact blocks
        # left: it is the expensive one, and it is the one that over-returns.
        remaining = max(0, self.max_candidates - len(hits))
        if remaining:
            for doc_id, _score in self._trigram.query(
                self._trigram_value(record), limit=remaining * 2
            ):
                note(doc_id, BLOCK_TRIGRAM)

        ranked = sorted(hits.items(), key=lambda item: (-len(item[1]), item[0]))
        return [
            Candidate(provider_id=self._provider_ids[doc_id], blocking_keys=tuple(blocks))
            for doc_id, blocks in ranked[: self.max_candidates]
        ]

    # -- introspection ----------------------------------------------------
    def normalized_provider(self, provider_id: str) -> NormalizedRecord | None:
        doc_id = self._doc_of.get(provider_id)
        return None if doc_id is None else self._normalized[doc_id]

    def block_key_counts(self) -> dict[str, int]:
        return {block: len(index) for block, index in self._indexes.items()}

    def __len__(self) -> int:
        return len(self._provider_ids)
