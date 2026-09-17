"""Fitting a scoring configuration from a prepared dataset.

Four steps, in this order and for this reason:

1. **Collect comparison vectors** over every candidate pair, split by model
   kind. Individuals and organizations are kept apart from here on and never
   meet again (PLAN 11.2).
2. **Fit m, u and lambda by EM**, independently per kind. This step is
   unsupervised - it never sees the ground truth - which is what makes the
   claim "no labels needed" true rather than decorative.
3. **Score every record with the uncalibrated model** to get one match weight
   per record: that of its best candidate. The weight rather than the posterior,
   because the posterior saturates - see `calibration.py` for why that matters
   more than it sounds like it should.
4. **Calibrate and choose thresholds** on the labelled split. This is the only
   step that touches ground truth, and it touches it through a deterministic
   fit/holdout split, with every reported number coming from the holdout.

The unit of calibration is worth being explicit about. It would be easier to
calibrate over all quarter of a million candidate pairs, and the resulting ECE
would look wonderful, because the overwhelming majority of pairs score near
zero and are correctly near zero. It would also be meaningless: no decision is
ever made about those pairs. Calibrating the *top candidate per record* is the
harder measurement and the honest one, because that is the record the
thresholds are applied to.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from concordance import __version__
from concordance.domain import Outcome
from concordance.eval.pairs import PreparedDataset, RecordWork
from concordance.logging_setup import get_logger
from concordance.matching.calibration import (
    DEFAULT_BINS,
    DEFAULT_HOLDOUT,
    DEFAULT_RESOLUTION,
    CalibrationResult,
    IsotonicCalibrator,
    Thresholds,
    calibrate,
)
from concordance.matching.comparators import ModelKind, as_organization, compare, pair_kind
from concordance.matching.fellegi_sunter import (
    DEFAULT_RESTARTS,
    DegenerateFitError,
    FellegiSunterModel,
    PatternCounts,
    fit_em,
)
from concordance.matching.scorer import MatchingEngine, ModelBundle
from concordance.matching.scoring_config import ScoringConfig, config_filename, now_iso

log = get_logger("eval.fitting")

__all__ = ["FitReport", "collect_patterns", "config_filename", "fit_config"]


@dataclass
class FitReport:
    """What the fit did, beyond the configuration it produced."""

    config: ScoringConfig
    models: dict[ModelKind, FellegiSunterModel] = field(default_factory=dict)
    calibration: dict[ModelKind, CalibrationResult] = field(default_factory=dict)
    pattern_counts: dict[ModelKind, int] = field(default_factory=dict)
    pair_counts: dict[ModelKind, int] = field(default_factory=dict)
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"fitted in {self.seconds:.2f}s  engine {self.config.engine_version}"]
        for kind in (ModelKind.INDIVIDUAL, ModelKind.ORGANIZATION):
            model = self.models.get(kind)
            if model is None:
                out.append(f"{kind:<13} not fitted")
                continue
            cal = self.calibration[kind]
            t = cal.thresholds
            out.append(
                f"{kind:<13} pairs={model.n_pairs:<7} patterns={model.n_patterns:<6} "
                f"lambda={model.lam:.4f} iters={model.iterations}"
            )
            out.append(
                f"{'':<13} ECE {cal.before.ece:.4f} -> {cal.after.ece:.4f}   "
                f"Brier {cal.before.brier:.4f} -> {cal.after.brier:.4f}   "
                f"holdout n={cal.n_holdout}"
            )
            out.append(
                f"{'':<13} accept>={t.t_auto_accept:.4f} reject<{t.t_auto_reject:.4f}  "
                f"grey band {t.grey_band_fraction:.1%} of volume  "
                f"precision {t.achieved_precision:.4f}"
            )
        for note in self.notes:
            out.append(f"note: {note}")
        return out


# --------------------------------------------------------------------------
# step 1 - comparison vectors
# --------------------------------------------------------------------------


def collect_patterns(dataset: Sequence[RecordWork]) -> dict[ModelKind, PatternCounts]:
    """Every candidate pair as a comparison vector, bucketed by model kind.

    A cross-type pair - the source filed an organization as a person, or the
    reverse - is coerced to the organization model and counted there rather
    than dropped. Dropping it would make `org_type_disagreement` unmatchable by
    construction, and counting it under the individual model is exactly the
    contamination PLAN 11.2 forbids.
    """
    buckets: dict[ModelKind, list[tuple[int, ...]]] = {
        ModelKind.INDIVIDUAL: [],
        ModelKind.ORGANIZATION: [],
    }
    for work in dataset:
        for provider, _ in work.candidates:
            kind = pair_kind(work.normalized, provider)
            if kind is None:
                left = as_organization(work.normalized)
                right = as_organization(provider)
                buckets[ModelKind.ORGANIZATION].append(compare(left, right, ModelKind.ORGANIZATION))
                continue
            buckets[kind].append(compare(work.normalized, provider, kind))
    return {kind: PatternCounts.from_vectors(kind, vectors) for kind, vectors in buckets.items()}


# --------------------------------------------------------------------------
# step 3 - labelled top-candidate posteriors
# --------------------------------------------------------------------------


def _label_of(work: RecordWork, provider_id: str | None) -> int:
    """1 when the proposed provider is a defensible answer for this record.

    A `NO_MATCH` record has no defensible provider, so every candidate is a 0 -
    which is what teaches the calibrator where the confidence of a wrong answer
    actually sits, and therefore where the reject threshold belongs.
    """
    if work.truth is None or provider_id is None:
        return 0
    if work.truth.expected_outcome is Outcome.NO_MATCH:
        return 0
    return 1 if provider_id in work.expected_provider_ids else 0


def _provisional_engine(models: dict[ModelKind, FellegiSunterModel]) -> MatchingEngine:
    """The engine as it stands before calibration: raw weights, no band.

    Used only to produce the match weights the calibrator is then fitted on.
    The calibrator passes weights through unchanged so the candidate ranking is
    exact, and the thresholds are infinite so nothing routes anywhere - this
    engine exists to score, not to decide.
    """
    open_band = Thresholds(
        t_auto_accept=float("inf"),
        t_auto_reject=float("-inf"),
        target_precision=1.0,
        target_recall=1.0,
        achieved_precision=0.0,
        achieved_recall=0.0,
        grey_band_fraction=1.0,
        n_holdout=0,
    )
    bundles = {
        kind: ModelBundle(model, IsotonicCalibrator.passthrough(), open_band)
        for kind, model in models.items()
    }
    return MatchingEngine(
        individual=bundles[ModelKind.INDIVIDUAL],
        organization=bundles[ModelKind.ORGANIZATION],
    )


@dataclass(frozen=True, slots=True)
class LabelledPosterior:
    """One labelled point for the calibrator: `value` is a match weight."""

    key: str
    kind: ModelKind
    value: float
    posterior: float
    label: int
    rank: int = 1


def labelled_posteriors(
    dataset: Sequence[RecordWork], engine: MatchingEngine, runner_up_depth: int = 3
) -> tuple[list[LabelledPosterior], list[LabelledPosterior]]:
    """The decision points, and the runner-up points that only widen the fit split.

    Rank 1 is the decision: it is the candidate the engine would accept, so it
    is the population the calibrator, the metrics and the thresholds all have
    to describe. Ranks 2 to `runner_up_depth` are returned separately and are
    *not* calibrated on - folding them in was tried and made holdout ECE
    markedly worse, because they are almost all negatives and shift the fitted
    map toward the wrong conditional. They are kept because the evaluation
    report needs the score distribution of near misses to show the margin
    check earning its place.
    """
    decisions: list[LabelledPosterior] = []
    runners_up: list[LabelledPosterior] = []
    for work in dataset:
        if not work.candidates:
            continue
        result = engine.score_record(work.normalized, work.candidates)
        for candidate in result.candidates[:runner_up_depth]:
            point = LabelledPosterior(
                key=work.record_id,
                kind=candidate.kind,
                value=candidate.match_weight,
                posterior=candidate.posterior,
                label=_label_of(work, candidate.provider_id),
                rank=candidate.rank,
            )
            (decisions if candidate.rank == 1 else runners_up).append(point)
    return decisions, runners_up


# --------------------------------------------------------------------------
# the whole fit
# --------------------------------------------------------------------------


def fit_config(
    prepared: PreparedDataset,
    seed: int,
    target_precision: float = 0.99,
    target_recall: float = 0.99,
    restarts: int = DEFAULT_RESTARTS,
    holdout_fraction: float = DEFAULT_HOLDOUT,
    bins: int = DEFAULT_BINS,
    resolution: float = DEFAULT_RESOLUTION,
    top_k: int = 5,
    margin_delta: float = 0.05,
) -> FitReport:
    """EM, then calibration, then thresholds - twice, independently."""
    started = time.perf_counter()
    notes: list[str] = []

    patterns = collect_patterns(prepared.work)
    models: dict[ModelKind, FellegiSunterModel] = {}
    for kind, data in patterns.items():
        try:
            models[kind] = fit_em(data, seed=seed, restarts=restarts)
        except DegenerateFitError as exc:
            # The organization model routinely has an order of magnitude fewer
            # pairs. Refusing to serve a fit made on too few of them is the
            # correct failure; substituting the individual model would be the
            # contamination this design exists to prevent.
            raise DegenerateFitError(
                f"{kind} model could not be fitted on this dataset: {exc}"
            ) from exc
        log.info(
            "em.fitted",
            model=str(kind),
            pairs=data.total,
            patterns=len(data.patterns),
            lam=round(models[kind].lam, 5),
            iterations=models[kind].iterations,
        )

    provisional = _provisional_engine(models)
    labelled, _runners_up = labelled_posteriors(prepared.work, provisional)

    bundles: dict[ModelKind, ModelBundle] = {}
    calibrations: dict[ModelKind, CalibrationResult] = {}
    for kind, model in models.items():
        subset = [p for p in labelled if p.kind is kind]
        result = calibrate(
            keys=[p.key for p in subset],
            weights=[p.value for p in subset],
            labels=[p.label for p in subset],
            seed=seed,
            raw_probabilities=[p.posterior for p in subset],
            holdout_fraction=holdout_fraction,
            bins=bins,
            target_precision=target_precision,
            target_recall=target_recall,
            resolution=resolution,
        )
        calibrations[kind] = result
        notes.extend(f"{kind}: {n}" for n in result.notes)
        bundles[kind] = ModelBundle(
            model=model,
            calibrator=result.calibrator,
            thresholds=result.thresholds,
            calibration=result.as_dict(),
        )
        log.info(
            "calibrated",
            model=str(kind),
            ece_before=round(result.before.ece, 5),
            ece_after=round(result.after.ece, 5),
            accept=round(result.thresholds.t_auto_accept, 4),
            reject=round(result.thresholds.t_auto_reject, 4),
        )

    config = ScoringConfig(
        config_id=config_filename(prepared.corruption_level or 0.0, seed).removesuffix(".json"),
        fitted_at=now_iso(),
        fitted_from="em",
        engine_version=__version__,
        seed=seed,
        top_k=top_k,
        margin_delta=margin_delta,
        bundles=bundles,
        dataset=prepared.summary(),
        notes=notes,
    )
    return FitReport(
        config=config,
        models=models,
        calibration=calibrations,
        pattern_counts={k: len(v.patterns) for k, v in patterns.items()},
        pair_counts={k: v.total for k, v in patterns.items()},
        seconds=time.perf_counter() - started,
        notes=notes,
    )


def summarize_weights(model: FellegiSunterModel, top: int = 3) -> list[dict[str, Any]]:
    """The strongest and weakest evidence the fit found, for the report."""
    rows: list[dict[str, Any]] = []
    for name, weights in model.weights().items():
        for level, weight in enumerate(weights):
            rows.append(
                {
                    "field": name,
                    "level": level,
                    "m": round(model.m[name][level], 6),
                    "u": round(model.u[name][level], 6),
                    "weight": round(weight, 4),
                }
            )
    rows.sort(key=lambda r: -abs(float(r["weight"])))
    return rows[: top * len(model.fields)]
