"""The corruption sweep: ten levels by four strategies, in minutes.

The sweep is what turns "the probabilistic engine is better" into a chart. It
regenerates the dataset at each corruption level from 0.0 to 0.9, refits the
scoring configuration on that level, and runs all four strategies against it.

Two decisions make it finish in minutes rather than hours, and both are the
point rather than an optimization:

**The dataset and the blocking index are built once per level, not once per
cell.** Normalizing 55,000 records and indexing them is the expensive half of
the work; four strategies asking for it separately would quadruple the run for
no new information. `PreparedDataset` exists so that cost is paid once.

**Levels run in parallel processes.** They are completely independent - separate
datasets, separate fits - so there is nothing to share and nothing to lock. The
worker count is deliberately conservative: each level holds a 50,000-provider
blocking index in memory, so the limit is RAM, not cores.

The configuration is refitted at every level rather than fitted once and reused.
That is the harder and more honest test: a real deployment refits when its data
changes, and a sweep that reuses one config measures how well that config
transfers, which is a different question.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from concordance.eval.fitting import fit_config
from concordance.eval.harness import evaluate
from concordance.eval.pairs import prepare
from concordance.logging_setup import get_logger
from concordance.matching.fellegi_sunter import DegenerateFitError
from concordance.matching.strategies import ALL_STRATEGIES, StrategyName, build_strategy

log = get_logger("eval.sweep")

DEFAULT_LEVELS: tuple[float, ...] = tuple(round(i / 10, 1) for i in range(10))
# Each worker holds a 50k-provider blocking index; the ceiling is memory, not
# cores, so the default stays well under the core count on a normal machine.
DEFAULT_WORKERS = min(os.cpu_count() or 1, 4)


@dataclass
class SweepResult:
    cells: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    levels: tuple[float, ...] = DEFAULT_LEVELS
    strategies: tuple[str, ...] = ()
    workers: int = DEFAULT_WORKERS
    providers: int = 0
    sanctions: int = 0
    seed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "levels": list(self.levels),
            "strategies": list(self.strategies),
            "workers": self.workers,
            "providers": self.providers,
            "sanctions": self.sanctions,
            "seed": self.seed,
            "seconds": round(self.seconds, 2),
            "cells": self.cells,
            "errors": self.errors,
        }

    def lines(self) -> list[str]:
        by_level: dict[float, dict[str, float]] = {}
        for cell in self.cells:
            level = float(cell["corruption_level"])
            by_level.setdefault(level, {})[str(cell["strategy"])] = float(cell["f1"])
        names = list(self.strategies)
        out = [
            f"sweep: {len(self.cells)} cells in {self.seconds:.1f}s across {self.workers} workers",
            "",
            f"{'corruption':<12}" + "".join(f"{n:>20}" for n in names),
        ]
        for level in sorted(by_level):
            row = by_level[level]
            out.append(
                f"{level:<12.1f}" + "".join(f"{row.get(n, float('nan')):>20.4f}" for n in names)
            )
        out.append("")
        out.append("(F1 by corruption level and strategy)")
        for error in self.errors:
            out.append(f"error: corruption {error['corruption']} - {error['error']}")
        return out


def _dataset_dir(root: Path, corruption: float) -> Path:
    return root / f"sweep-c{corruption:.1f}"


def _reusable(
    dataset: Path, seed: int, corruption: float, providers: int, sanctions: int
) -> bool:
    """Whether a cached sweep dataset still describes what would be generated.

    Reuse saves most of a sweep's wall clock, but a dataset left behind by an
    older generator will happily be measured as though it were current, and the
    numbers look plausible - that is the dangerous part. So the manifest has to
    agree on the generator version and on every parameter that shapes the data,
    not merely exist.
    """
    from concordance.synth.pipeline import GENERATOR_VERSION

    manifest = dataset / "manifest.json"
    if not (dataset / "providers.parquet").exists() or not manifest.exists():
        return False
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    counts = meta.get("counts") or {}
    return (
        meta.get("generator_version") == GENERATOR_VERSION
        and meta.get("seed") == seed
        and meta.get("corruption_level") == corruption
        and counts.get("providers") == providers
        and counts.get("sanction_records") == sanctions
    )


def run_level(
    corruption: float,
    seed: int,
    root: str,
    providers: int,
    sanctions: int,
    max_candidates: int,
    strategies: tuple[str, ...],
    target_precision: float,
    reuse: bool,
) -> list[dict[str, Any]]:
    """One corruption level, all strategies. Runs in a worker process.

    Module-level and taking only picklable arguments, because Windows spawns
    workers rather than forking them and a closure would not survive the trip.
    """
    from concordance.synth.pipeline import seed_dataset

    started = time.perf_counter()
    dataset = _dataset_dir(Path(root), corruption)
    if not reuse or not _reusable(dataset, seed, corruption, providers, sanctions):
        seed_dataset(
            providers=providers,
            sanctions=sanctions,
            corruption=corruption,
            seed=seed,
            out_dir=dataset,
            write_excel=False,
        )

    prepared = prepare(dataset, max_candidates=max_candidates, show_progress=False)
    fit = fit_config(prepared, seed=seed, target_precision=target_precision)
    engine = fit.config.engine()

    # The fit's own holdout measurement, before and after isotonic calibration.
    # It is the only place the uncalibrated posterior is measured, so it rides
    # along on the cells that use the fitted engine and the Lab's reliability
    # diagram can show what calibration changed rather than only where it ended.
    calibration_fit = {
        str(kind): {
            "before": result.before.as_dict(),
            "after": result.after.as_dict(),
            "thresholds": result.thresholds.as_dict(),
            "n_fit": result.n_fit,
            "n_holdout": result.n_holdout,
        }
        for kind, result in sorted(fit.calibration.items(), key=lambda item: str(item[0]))
    }

    rows: list[dict[str, Any]] = []
    for name in strategies:
        strategy = build_strategy(name, engine)
        report = evaluate(
            prepared,
            strategy,
            config_id=fit.config.config_id,
            seed=seed,
            keep_failures=0,
            show_progress=False,
        )
        row = report.as_dict()
        # The sweep payload is one row per cell and is read as a whole; the
        # per-record failure list would multiply its size by two orders of
        # magnitude for data nothing in the chart uses.
        row["detail"].pop("failures", None)
        row["detail"]["level_seconds"] = round(time.perf_counter() - started, 2)
        if name not in (StrategyName.DETERMINISTIC, StrategyName.FUZZY):
            row["detail"]["calibration_fit"] = calibration_fit
        rows.append(row)
    return rows


def _worker(args: tuple[Any, ...]) -> tuple[float, list[dict[str, Any]], str]:
    corruption = args[0]
    try:
        return corruption, run_level(*args), ""
    except DegenerateFitError as exc:
        # A level whose fit collapses is a result, not a crash: record it and
        # let the other nine finish.
        return corruption, [], f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        return corruption, [], f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"


def sweep(
    root: Path,
    seed: int,
    levels: tuple[float, ...] = DEFAULT_LEVELS,
    strategies: tuple[StrategyName, ...] = ALL_STRATEGIES,
    providers: int = 50_000,
    sanctions: int = 5_000,
    max_candidates: int = 50,
    target_precision: float = 0.99,
    workers: int = DEFAULT_WORKERS,
    reuse: bool = True,
    on_level: Callable[[float, int, int], None] | None = None,
) -> SweepResult:
    """Run every cell. Levels in parallel, strategies sequential within a level.

    `on_level(corruption, done, total)` is called in this process as each level
    finishes, which is what lets a job report progress on a run of minutes.
    """
    started = time.perf_counter()
    root.mkdir(parents=True, exist_ok=True)
    names = tuple(str(s) for s in strategies)
    result = SweepResult(
        levels=levels,
        strategies=names,
        workers=max(1, workers),
        providers=providers,
        sanctions=sanctions,
        seed=seed,
    )

    jobs = [
        (
            level,
            seed,
            str(root),
            providers,
            sanctions,
            max_candidates,
            names,
            target_precision,
            reuse,
        )
        for level in levels
    ]

    if result.workers == 1:
        outputs = []
        for job in jobs:
            outputs.append(_worker(job))
            if on_level is not None:
                on_level(job[0], len(outputs), len(jobs))
    else:
        outputs = []
        with ProcessPoolExecutor(max_workers=result.workers) as pool:
            futures = {pool.submit(_worker, job): job[0] for job in jobs}
            for future in as_completed(futures):
                outputs.append(future.result())
                log.info("sweep.level", corruption=futures[future], done=len(outputs), of=len(jobs))
                if on_level is not None:
                    on_level(futures[future], len(outputs), len(jobs))

    for corruption, rows, error in sorted(outputs, key=lambda o: o[0]):
        if error:
            result.errors.append({"corruption": corruption, "error": error})
        result.cells.extend(rows)

    result.seconds = time.perf_counter() - started
    log.info(
        "sweep.done",
        cells=len(result.cells),
        errors=len(result.errors),
        seconds=round(result.seconds, 1),
    )
    return result


def write_sweep(result: SweepResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
