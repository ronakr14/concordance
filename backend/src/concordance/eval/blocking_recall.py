"""Measuring blocking recall.

Blocking recall is the ceiling on system recall: a true pair that never becomes
a candidate cannot be scored, however good the model downstream is. So it is
measured, not assumed - and the report says which blocks carried which pairs,
and which corruption families are responsible for the misses. That last column
is what turns "recall is 96%" into a decision about which block to add.
"""

from __future__ import annotations

import json
import time
import tracemalloc
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from concordance.domain import Outcome
from concordance.logging_setup import Progress, get_logger
from concordance.matching.blocking import ALL_BLOCKS, InMemoryCandidateGenerator
from concordance.store.parquet_store import ParquetRecordStore

log = get_logger("eval.blocking")


@dataclass
class MissedPair:
    """One true pair the blocking step failed to propose."""

    record_id: str
    provider_id: str
    scenario: str
    families: tuple[str, ...]
    candidates_returned: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "provider_id": self.provider_id,
            "scenario": self.scenario,
            "corruption_families": list(self.families),
            "candidates_returned": self.candidates_returned,
        }


@dataclass
class RecallReport:
    corruption_level: float | None = None
    max_candidates: int = 0
    evaluated: int = 0
    found: int = 0
    candidate_counts: list[int] = field(default_factory=list)
    block_contribution: Counter[str] = field(default_factory=Counter)
    block_sole_contribution: Counter[str] = field(default_factory=Counter)
    misses_by_scenario: Counter[str] = field(default_factory=Counter)
    misses_by_family: Counter[str] = field(default_factory=Counter)
    recall_by_scenario: dict[str, tuple[int, int]] = field(default_factory=dict)
    missed: list[MissedPair] = field(default_factory=list)
    index_stats: dict[str, Any] = field(default_factory=dict)
    query_seconds: float = 0.0

    @property
    def recall(self) -> float:
        return self.found / self.evaluated if self.evaluated else 0.0

    @property
    def mean_candidates(self) -> float:
        return sum(self.candidate_counts) / len(self.candidate_counts) if self.candidate_counts else 0.0

    @property
    def p95_candidates(self) -> int:
        if not self.candidate_counts:
            return 0
        ordered = sorted(self.candidate_counts)
        return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]

    @property
    def max_candidates_seen(self) -> int:
        return max(self.candidate_counts, default=0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "corruption_level": self.corruption_level,
            "max_candidates": self.max_candidates,
            "evaluated": self.evaluated,
            "found": self.found,
            "recall": round(self.recall, 5),
            "mean_candidates": round(self.mean_candidates, 2),
            "p95_candidates": self.p95_candidates,
            "max_candidates_seen": self.max_candidates_seen,
            "block_contribution": dict(self.block_contribution.most_common()),
            "block_sole_contribution": dict(self.block_sole_contribution.most_common()),
            "recall_by_scenario": {
                k: {"found": v[0], "total": v[1], "recall": round(v[0] / v[1], 4) if v[1] else 0.0}
                for k, v in sorted(self.recall_by_scenario.items())
            },
            "misses_by_scenario": dict(self.misses_by_scenario.most_common()),
            "misses_by_corruption_family": dict(self.misses_by_family.most_common()),
            "index": self.index_stats,
            "query_seconds": round(self.query_seconds, 3),
        }

    def lines(self, sample_misses: int = 10) -> list[str]:
        """Human-readable report, for the CLI."""
        out = [
            f"blocking recall      {self.recall:.4f}  ({self.found}/{self.evaluated} true pairs)",
            f"candidates/record    mean {self.mean_candidates:.1f}   p95 {self.p95_candidates}   max {self.max_candidates_seen}   cap {self.max_candidates}",
            f"index build          {self.index_stats.get('build_seconds', '?')}s over {self.index_stats.get('providers', '?')} providers",
            f"index memory         {self.index_stats.get('resident_mb', 'not measured (--memory)')}"
            + (f" MB resident, {self.index_stats['peak_mb']} MB peak" if "peak_mb" in self.index_stats else ""),
            f"query time           {self.query_seconds:.2f}s",
            "",
            "per-block contribution (pairs the block found / found alone):",
        ]
        for block in ALL_BLOCKS:
            total = self.block_contribution.get(block, 0)
            sole = self.block_sole_contribution.get(block, 0)
            out.append(f"  {block:<18} {total:>7}  {sole:>7}")
        out.append("")
        out.append("recall by scenario:")
        for scenario, (found, total) in sorted(self.recall_by_scenario.items()):
            rate = found / total if total else 0.0
            flag = "  <-- " if rate < 0.98 else ""
            out.append(f"  {scenario:<24} {found:>5}/{total:<5} {rate:.4f}{flag}")
        if self.missed:
            out.append("")
            out.append(f"missed pairs by corruption family: {dict(self.misses_by_family.most_common())}")
            out.append(f"first {min(sample_misses, len(self.missed))} missed pairs:")
            for miss in self.missed[:sample_misses]:
                out.append(
                    f"  {miss.record_id} -> {miss.provider_id}  [{miss.scenario}]  "
                    f"families={list(miss.families)}  returned={miss.candidates_returned}"
                )
        return out


def measure_blocking_recall(
    dataset: Path | str,
    max_candidates: int = 50,
    trigram_floor: float = 0.3,
    limit: int | None = None,
    keep_misses: int = 200,
    measure_memory: bool = False,
) -> RecallReport:
    """Build the index, block every sanction record, compare against truth.

    An `AMBIGUOUS` record counts as found when *any* of its plausible providers
    is proposed: blocking's job is to put the decision in front of the scorer,
    not to make it.
    """
    store = ParquetRecordStore(dataset)
    truth = store.ground_truth()

    generator = InMemoryCandidateGenerator(max_candidates=max_candidates, trigram_floor=trigram_floor)
    # The index has to fit in a worker process alongside everything else, so
    # its footprint is measurable - but tracemalloc taxes every allocation and
    # inflates the build time several-fold, so it is off unless asked for. Run
    # the build timing and the memory measurement as separate passes.
    if measure_memory:
        tracemalloc.start()
    with Progress("blocking.index", total=store.provider_count()):
        generator.build(store)
    index_stats = generator.stats.as_dict()
    if measure_memory:
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        index_stats["resident_mb"] = round(current / 1024 / 1024, 1)
        index_stats["peak_mb"] = round(peak / 1024 / 1024, 1)
        index_stats["build_seconds_note"] = "inflated by tracemalloc; time a run without --memory"


    report = RecallReport(
        corruption_level=store.manifest.get("corruption_level"),
        max_candidates=max_candidates,
        index_stats=index_stats,
    )

    records = store.all_sanctions()
    if limit:
        records = records[:limit]

    started = time.perf_counter()
    with Progress("blocking.query", total=len(records), every=1000) as progress:
        for record in records:
            progress.tick()
            gt = truth.get(record.record_id)
            if gt is None or gt.expected_outcome is Outcome.NO_MATCH:
                # Nothing to find; these records still cost candidates, which
                # is why they are counted in the candidate statistics.
                candidates = generator.candidates(record)
                report.candidate_counts.append(len(candidates))
                continue

            expected = {gt.expected_provider_id} if gt.expected_provider_id else set()
            expected |= set(gt.corruption_profile.get("plausible_provider_ids") or [])
            expected.discard(None)
            if not expected:
                continue

            candidates = generator.candidates(record)
            report.candidate_counts.append(len(candidates))
            report.evaluated += 1

            scenario = gt.scenario_tag or "untagged"
            found_pair = next((c for c in candidates if c.provider_id in expected), None)
            hit, total = report.recall_by_scenario.get(scenario, (0, 0))

            if found_pair is not None:
                report.found += 1
                report.recall_by_scenario[scenario] = (hit + 1, total + 1)
                for block in found_pair.blocking_keys:
                    report.block_contribution[block] += 1
                if len(found_pair.blocking_keys) == 1:
                    report.block_sole_contribution[found_pair.blocking_keys[0]] += 1
            else:
                report.recall_by_scenario[scenario] = (hit, total + 1)
                families = tuple(
                    sorted({op["family"] for op in gt.corruption_profile.get("applied", [])})
                )
                report.misses_by_scenario[scenario] += 1
                for family in families or ("none",):
                    report.misses_by_family[family] += 1
                if len(report.missed) < keep_misses:
                    report.missed.append(
                        MissedPair(
                            record_id=record.record_id,
                            provider_id=sorted(expected)[0],
                            scenario=scenario,
                            families=families,
                            candidates_returned=len(candidates),
                        )
                    )

    report.query_seconds = time.perf_counter() - started
    log.info(
        "blocking.recall",
        recall=round(report.recall, 4),
        mean_candidates=round(report.mean_candidates, 2),
        p95=report.p95_candidates,
    )
    return report


def write_report(report: RecallReport, path: Path) -> Path:
    """Persist the JSON form beside the human-readable one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
