"""Turning a dataset into the candidate pairs everything else consumes.

Built once per dataset and reused by the fit, by every strategy, and by every
cell of the sweep at that corruption level. That reuse is what makes the sweep
finish in minutes: normalizing 55,000 records and building the blocking index
is the expensive half of a run, and doing it four times per corruption level
because four strategies asked separately would quadruple the whole exercise for
no new information.

The candidate pairs hold *references* to normalized providers, not copies, so a
quarter of a million pairs costs a quarter of a million pointers.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from concordance.config import Settings
from concordance.db.retry import with_reconnect
from concordance.domain import Candidate, GroundTruth, SanctionRecord
from concordance.logging_setup import Progress, get_logger
from concordance.matching.blocking import InMemoryCandidateGenerator
from concordance.matching.normalization import NormalizedRecord, normalize_sanction
from concordance.store.parquet_store import ParquetRecordStore

log = get_logger("eval.pairs")

CandidatePair = tuple[NormalizedRecord, Candidate]


@dataclass
class RecordWork:
    """One sanction record with everything needed to score it."""

    record: SanctionRecord
    normalized: NormalizedRecord
    candidates: list[CandidatePair]
    truth: GroundTruth | None

    @property
    def record_id(self) -> str:
        return self.record.record_id

    @property
    def expected_provider_ids(self) -> set[str]:
        """Every provider id that would be a defensible answer.

        For an `AMBIGUOUS` record that is the whole plausible set: blocking and
        scoring are not being asked to pick one, they are being asked to notice
        that more than one fits.
        """
        if self.truth is None:
            return set()
        out = {self.truth.expected_provider_id} if self.truth.expected_provider_id else set()
        out |= set(self.truth.corruption_profile.get("plausible_provider_ids") or [])
        return {pid for pid in out if pid}


@dataclass
class PreparedDataset:
    """A dataset, normalized and blocked once, ready for any strategy."""

    path: Path
    corruption_level: float | None
    work: list[RecordWork]
    index_stats: dict[str, Any]
    provider_snapshot_hash: str
    sanction_snapshot_hash: str
    provider_count: int
    prepare_seconds: float = 0.0
    # Seconds per pipeline stage, so "mean latency per stage" is a measurement
    # rather than one number covering three different kinds of work.
    stage_seconds: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def __iter__(self) -> Iterator[RecordWork]:
        return iter(self.work)

    def __len__(self) -> int:
        return len(self.work)

    @property
    def mean_candidates(self) -> float:
        return sum(len(w.candidates) for w in self.work) / len(self.work) if self.work else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "dataset": str(self.path),
            "corruption_level": self.corruption_level,
            "records": len(self.work),
            "providers": self.provider_count,
            "mean_candidates": round(self.mean_candidates, 2),
            "provider_snapshot_hash": self.provider_snapshot_hash,
            "sanction_snapshot_hash": self.sanction_snapshot_hash,
            "index": self.index_stats,
            "prepare_seconds": round(self.prepare_seconds, 2),
            "stage_seconds": {k: round(v, 4) for k, v in self.stage_seconds.items()},
        }


def prepare(
    dataset: Path | str,
    max_candidates: int = 50,
    trigram_floor: float = 0.3,
    limit: int | None = None,
    show_progress: bool = True,
) -> PreparedDataset:
    """Normalize, index and block a whole dataset."""
    started = time.perf_counter()
    path = Path(dataset)
    store = ParquetRecordStore(path)
    truth = store.ground_truth()

    generator = InMemoryCandidateGenerator(
        max_candidates=max_candidates, trigram_floor=trigram_floor
    )
    index_started = time.perf_counter()
    with Progress("prepare.index", total=store.provider_count(), enabled=show_progress):
        generator.build(store)
    index_seconds = time.perf_counter() - index_started

    records = store.all_sanctions()
    if limit:
        records = records[:limit]

    work: list[RecordWork] = []
    normalize_seconds = 0.0
    block_seconds = 0.0
    with Progress(
        "prepare.block", total=len(records), every=1000, enabled=show_progress
    ) as progress:
        for record in records:
            progress.tick()
            mark = time.perf_counter()
            normalized = normalize_sanction(record)
            normalize_seconds += time.perf_counter() - mark

            mark = time.perf_counter()
            pairs: list[CandidatePair] = []
            for candidate in generator.candidates_for(normalized):
                provider = generator.normalized_provider(candidate.provider_id)
                if provider is not None:
                    pairs.append((provider, candidate))
            block_seconds += time.perf_counter() - mark
            work.append(RecordWork(record, normalized, pairs, truth.get(record.record_id)))

    prepared = PreparedDataset(
        path=path,
        corruption_level=store.manifest.get("corruption_level"),
        work=work,
        index_stats=generator.stats.as_dict(),
        provider_snapshot_hash=store.snapshot_hash(),
        sanction_snapshot_hash=store.sanction_snapshot_hash(),
        provider_count=store.provider_count(),
        prepare_seconds=time.perf_counter() - started,
        stage_seconds={
            "index": index_seconds,
            "normalize": normalize_seconds,
            "block": block_seconds,
        },
    )
    log.info(
        "prepare.done",
        records=len(work),
        providers=prepared.provider_count,
        mean_candidates=round(prepared.mean_candidates, 2),
        seconds=round(prepared.prepare_seconds, 2),
    )
    return prepared


@dataclass
class PostgresStream:
    """Chunked normalization and blocking against Postgres.

    One code path, two consumers. `prepare_from_postgres` flattens it to answer
    "does the storage swap change any number the engine produces"; the
    reconciliation run consumes it chunk by chunk so a five-thousand-record job
    persists as it goes rather than holding every candidate list in memory
    until the end.

    Chunking is not a tuning knob, it is the difference between three minutes
    and most of a day: the SQL generator answers a chunk in two queries and a
    single record in two queries, and over a network link the round trips
    dominate everything else.
    """

    session: Any
    store: Any
    generator: Any
    total: int
    truth: dict[str, GroundTruth]
    chunk_size: int = 500
    show_progress: bool = True
    normalize_seconds: float = 0.0
    block_seconds: float = 0.0
    fetch_seconds: float = 0.0

    def __len__(self) -> int:
        return self.total

    def chunks(self) -> Iterator[list[RecordWork]]:
        with Progress(
            "prepare.block", total=self.total, every=1000, enabled=self.show_progress
        ) as progress:
            for start in range(0, self.total, self.chunk_size):
                # A chunk at a time rather than one query for the whole file:
                # a hosted database that closes a long-running connection turns
                # a single five-thousand-row fetch into a hang, and a short
                # statement that has to be retried costs one chunk.
                # Clamped to what is left, not the chunk size: a run limited to
                # forty records asked for five hundred and scored all of them.
                size = min(self.chunk_size, self.total - start)
                mark = time.perf_counter()
                chunk = with_reconnect(
                    self.session,
                    partial(self.store.sanction_batch, start, size),
                    what="sanction chunk",
                )
                self.fetch_seconds += time.perf_counter() - mark
                if not chunk:
                    break

                mark = time.perf_counter()
                normalized = [normalize_sanction(record) for record in chunk]
                self.normalize_seconds += time.perf_counter() - mark

                mark = time.perf_counter()
                candidate_lists = with_reconnect(
                    self.session,
                    partial(self.generator.candidates_batch, normalized),
                    what="blocking chunk",
                )
                wanted = [c.provider_id for lst in candidate_lists for c in lst]
                providers = with_reconnect(
                    self.session,
                    partial(self.generator.normalized_providers, wanted),
                    what="candidate providers",
                )
                work: list[RecordWork] = []
                for record, norm, candidates in zip(chunk, normalized, candidate_lists, strict=True):
                    progress.tick()
                    pairs: list[CandidatePair] = [
                        (providers[c.provider_id], c)
                        for c in candidates
                        if c.provider_id in providers
                    ]
                    work.append(RecordWork(record, norm, pairs, self.truth.get(record.record_id)))
                self.block_seconds += time.perf_counter() - mark
                yield work

    @property
    def stage_seconds(self) -> dict[str, float]:
        return {
            "index": 0.0,  # the index is a table, written by the loader
            "fetch": self.fetch_seconds,
            "normalize": self.normalize_seconds,
            "block": self.block_seconds,
        }


def stream_from_postgres(
    session: Any,
    max_candidates: int = 50,
    trigram_floor: float = 0.3,
    limit: int | None = None,
    chunk_size: int = 500,
    with_truth: bool = True,
    show_progress: bool = True,
) -> PostgresStream:
    """The blocking half of a Postgres run, ready to be iterated in chunks.

    `with_truth=False` for a real reconciliation: production data has no ground
    truth, and loading a table that is empty there would make the evaluation
    path and the production path differ in a way nobody notices until it does.
    """
    from concordance.store.postgres_store import PostgresRecordStore
    from concordance.store.sql_candidates import SqlCandidateGenerator

    store = PostgresRecordStore(session)
    generator = SqlCandidateGenerator(
        session=session, max_candidates=max_candidates, trigram_floor=trigram_floor
    )
    # `limit` bounds the fetch rather than the list afterwards: a smoke run
    # over twenty-five records should not pull five thousand rows across the
    # wire to throw all but twenty-five of them away.
    total = store.sanction_count()
    return PostgresStream(
        session=session,
        store=store,
        generator=generator,
        total=min(total, limit) if limit else total,
        truth=store.ground_truth() if with_truth else {},
        chunk_size=chunk_size,
        show_progress=show_progress,
    )


def prepare_from_postgres(
    session: Any,
    dataset: Path | str,
    max_candidates: int = 50,
    trigram_floor: float = 0.3,
    limit: int | None = None,
    chunk_size: int = 500,
    show_progress: bool = True,
) -> PreparedDataset:
    """`prepare` against Postgres instead of Parquet and the in-memory index.

    This exists to answer one question: does the storage swap change any
    number the engine produces? It therefore differs from `prepare` in exactly
    two places - where records come from, and which generator blocks them -
    and shares every other line, including the normalization and the ordering.
    A metric that moves between the two is a bug in the seam, which is what
    GATE 5 is asking.
    """
    started = time.perf_counter()
    path = Path(dataset)
    stream = stream_from_postgres(
        session,
        max_candidates=max_candidates,
        trigram_floor=trigram_floor,
        limit=limit,
        chunk_size=chunk_size,
        show_progress=show_progress,
    )

    work: list[RecordWork] = []
    for batch in stream.chunks():
        work.extend(batch)

    prepared = PreparedDataset(
        path=path,
        corruption_level=ParquetRecordStore(path).manifest.get("corruption_level"),
        work=work,
        index_stats=stream.generator.stats.as_dict(),
        provider_snapshot_hash=stream.store.snapshot_hash(),
        sanction_snapshot_hash=stream.store.sanction_snapshot_hash(),
        provider_count=stream.store.provider_count(),
        prepare_seconds=time.perf_counter() - started,
        stage_seconds=stream.stage_seconds,
    )
    log.info(
        "prepare.done",
        source="postgres",
        records=len(work),
        providers=prepared.provider_count,
        mean_candidates=round(prepared.mean_candidates, 2),
        seconds=round(prepared.prepare_seconds, 2),
    )
    return prepared


def prepare_from_settings(
    settings: Settings, dataset: Path | None = None, limit: int | None = None
) -> PreparedDataset:
    return prepare(
        dataset or settings.generated_dir,
        max_candidates=settings.MAX_CANDIDATES_PER_RECORD,
        limit=limit,
    )
