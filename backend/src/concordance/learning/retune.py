"""Retuning a scoring config on reviewer labels.

A retune takes three things - the config that is live now (the *parent*), one
run's whole candidate-pair tally, and every label reviewers have given - and
returns a new config. It never edits the parent, and it never activates the new
one: that is a separate decision with its own audit row.

Three steps, each with a reason:

1. **Split the labels once**, by a hash of the result they belong to, into a
   fit split and a holdout. Everything the new config learns comes from the fit
   split; everything it is judged on comes from the holdout. The same split is
   used for the parent, so "the retune improved precision" compares two configs
   on the same records.
2. **Semi-supervised EM** (`fellegi_sunter.fit_em` with `clamped`) over the
   run's tally, with the fit split's labelled pairs clamped to their labels and
   the parent's tables as the first restart's starting point. Labelled pairs
   that came from the tally's own run are taken out of its unlabelled counts
   first, so no pair is counted twice.
3. **Recalibrate** on the labels, each counted at its weight (1 / the
   probability it was reviewed): isotonic on the fit split.
4. **Re-threshold on the population**, not on the labels. Every record of the
   run is rescored under the new model and calibrator, and the thresholds are
   where the *expected* precision and recall of that population meet their
   targets (`calibration.expected_thresholds`). A few hundred labels, nineteen
   in twenty of them matches, cannot place a 99% precision threshold - it
   comes down to whether one or two negatives fell in the holdout, and in
   simulation the accept threshold jumped round to round and took recall with
   it. The run has thousands of records; the labels' job is the calibration.
   Without a population, the label holdout picks the thresholds instead.
5. **Judge both configs on the label holdout** - the only step that is
   evidence rather than fitting - and say whether the numbers justify
   switching (`recommendation`).

**A retune can be worse than its parent, and often is early on.** A few hundred
labels that are a few percent wrong cannot certify a 99% precision threshold:
the observed label rate at the top of the range is as consistent with "all
matches, some verdicts wrong" as with "a few percent are not matches". A retune
that believes the second sends most of the volume to review. That is the honest
reading of thin evidence, not a bug - but it is not a config to switch to, so
the retune measures both configs on the same held-out labels and recommends
`keep` unless the new one holds precision without buying it with review load.
Nothing activates on its own.

**Reviewers make mistakes**, at a rate `label_noise` (`REVIEWER_ERROR_RATE`),
and ignoring that breaks the loop rather than blurring it. On labels that are
3% wrong even a perfect model shows 97% precision, so a 99% target is never
met, the accept threshold climbs to the top, and recall collapses - which is
exactly what the first simulation did. So the rate enters both halves: EM
treats each verdict as evidence about the class rather than the class itself,
and calibration and the threshold search count each verdict as its de-noised
value, so every precision they compute is an estimate of the true one. The rate
is configured, not estimated - it is what a QA re-review of a sample of
verdicts measures, and the docs say how wrong it can be before it matters.

**The label bias, and what does and does not correct it.** Reviewers see what
the engine sends them: every accepted match (a case needs an approval) and the
grey band, plus whatever they choose to open. They do not see the auto-rejects,
which is exactly where a missed sanction would sit. Two things answer that:

- For m and u, nothing needs correcting. Whether a pair was reviewed depends on
  its scores and a random draw - things the system observed - so the labels are
  missing at random given the data, and the likelihood semi-supervised EM
  maximizes is unbiased without weights.
- For calibration and thresholds it does matter, because they are statements
  about records, and the labelled records over-represent the top of the score
  range. The random audit of auto-rejects fills in the bottom with labels of
  known inclusion probability, weighted up by 1 / `AUDIT_RATE`. Reviews of
  auto-rejects that were *not* drawn by the audit have an unknown inclusion
  probability: they are clamped in EM, where missing-at-random covers them, and
  left out of calibration, where a weight would have to be invented.

A model kind with too few labels of its own keeps the parent's bundle
unchanged and says so. Refitting the organization model on nine labels would
be a guess with a version number.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from concordance import __version__
from concordance.matching.calibration import (
    DEFAULT_BINS,
    DEFAULT_RESOLUTION,
    calibrate,
    denoise,
    expected_thresholds,
    in_holdout,
    reliability,
)
from concordance.matching.comparators import (
    FIELDS_BY_KIND,
    ComparisonVector,
    ModelKind,
)
from concordance.matching.fellegi_sunter import (
    DEFAULT_RESTARTS,
    ClampedPatterns,
    DegenerateFitError,
    PatternCounts,
    fit_em,
)
from concordance.matching.scorer import ModelBundle
from concordance.matching.scoring_config import ScoringConfig, now_iso

#: Share of labels held out to judge the retune against its parent.
DEFAULT_HOLDOUT = 0.3
#: Below this many labels of its own, a model kind keeps its parent's bundle.
MIN_PER_KIND = 30
#: Label sources. Only `voluntary` is left out of calibration - see the docstring.
SOURCES = ("review", "audit", "voluntary")


class NotEnoughLabelsError(ValueError):
    """Not enough labels to fit on. Carries the counts so the caller can say why."""

    def __init__(self, message: str, counts: dict[str, int]) -> None:
        super().__init__(message)
        self.counts = counts


@dataclass(frozen=True, slots=True)
class Label:
    """One reviewer verdict on one candidate pair."""

    #: Stable identity for the fit/holdout split - the match result id.
    key: str
    kind: ModelKind
    vector: ComparisonVector
    #: 1 for `TRUE_MATCH`, 0 for `FALSE_MATCH`.
    label: int
    #: 1 / the probability this pair reached a reviewer.
    weight: float = 1.0
    source: str = "review"
    #: The pair is one of the tally's own pairs (the label came from the run
    #: the tally was taken from), so it is removed from the unlabelled counts.
    in_tally: bool = False


def vector_from_levels(kind: ModelKind, levels: Mapping[str, Any]) -> ComparisonVector | None:
    """A comparison vector from the level names a candidate row stores.

    `None` when a field is missing or a name is unknown - a label whose
    evidence cannot be reconstructed exactly is not used, rather than guessed.
    """
    out: list[int] = []
    for spec in FIELDS_BY_KIND[kind]:
        name = levels.get(spec.name)
        if name is None:
            return None
        try:
            out.append(int(spec.levels[str(name)]))
        except KeyError:
            return None
    return tuple(out)


# --------------------------------------------------------------------------
# judging a bundle on labels
# --------------------------------------------------------------------------


def judge(bundle: ModelBundle, labels: Sequence[Label], noise: float = 0.0) -> dict[str, Any]:
    """How one bundle's decisions score against labelled pairs, weighted.

    - `precision`: of what it would auto-accept, the share that is a true match.
    - `recall`: of the true matches, the share it would auto-accept.
    - `missed`: of the true matches, the share it would auto-*reject* - the
      number that matters most in a sanctions system, because nobody looks.
    - `review_share`: the share it would send to a person.

    Counted on de-noised labels, so each is an estimate of the true rate.
    """
    return _judge(labels, lambda _x: bundle, noise)


def judge_engine(engine: Any, labels: Sequence[Label], noise: float = 0.0) -> dict[str, Any]:
    """`judge` across both model kinds, each label under its own kind's bundle."""
    return _judge(labels, lambda x: engine.bundle(x.kind), noise)


def _judge(
    labels: Sequence[Label], bundle_for: Callable[[Label], ModelBundle], noise: float
) -> dict[str, Any]:
    truth = denoise([x.label for x in labels], noise)
    calibrated: list[float] = []
    total = accepted = accepted_true = positives = missed = review = 0.0
    for x, y in zip(labels, truth, strict=True):
        bundle = bundle_for(x)
        conf = bundle.calibrator.apply(bundle.model.match_weight(x.vector))
        calibrated.append(conf)
        w = x.weight
        total += w
        positives += w * y
        if conf >= bundle.thresholds.t_auto_accept:
            accepted += w
            accepted_true += w * y
        elif conf < bundle.thresholds.t_auto_reject:
            missed += w * y
        else:
            review += w
    metrics = reliability(calibrated, truth, DEFAULT_BINS, [x.weight for x in labels])
    return {
        "n": len(labels),
        "precision": _ratio(accepted_true, accepted),
        "recall": _ratio(accepted_true, positives),
        "missed": _ratio(missed, positives),
        "review_share": _ratio(review, total),
        "ece": round(metrics.ece, 6),
        "brier": round(metrics.brier, 6),
    }


def _ratio(a: float, b: float) -> float | None:
    """A rate, clipped: de-noised counts can put an estimate a hair past 0 or 1."""
    return round(min(max(a / b, 0.0), 1.0), 6) if b > 0 else None


# --------------------------------------------------------------------------
# the retune
# --------------------------------------------------------------------------


#: How much worse a holdout number may be before it counts as worse. Below
#: this, a few hundred labels cannot tell the two configs apart anyway.
TOLERANCE = 0.01
#: The same, for true matches auto-rejected. Wider, because a narrower grey
#: band always moves a few records both ways and the rest of the trade -
#: fewer reviews, more auto-accepts - can be worth a point of it. Not wider
#: still: an auto-rejected match is the error nobody ever sees.
MISSED_TOLERANCE = 0.02


@dataclass
class RetuneResult:
    config: ScoringConfig
    metrics: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    @property
    def improved(self) -> bool | None:
        """Whether holdout precision went up, where both configs accepted anything."""
        overall = self.metrics.get("holdout", {})
        new = (overall.get("new") or {}).get("precision")
        old = (overall.get("parent") or {}).get("precision")
        if new is None or old is None:
            return None
        return bool(new > old)

    @property
    def recommended(self) -> bool:
        """Whether the holdout says activating this is an improvement."""
        return bool(self.metrics.get("recommendation", {}).get("activate"))

    @property
    def verdict(self) -> str:
        return str(self.metrics.get("recommendation", {}).get("reason", "no holdout to judge on"))


def recommend(new: dict[str, Any], parent: dict[str, Any], target_precision: float) -> dict[str, Any]:
    """Whether to activate, from the two configs' numbers on the same labels.

    Activate when the new config gives up no precision worth the name, misses
    no more true matches, and buys neither with review load - and improves at
    least one of them. Anything less is a `keep`: the parent is in production
    and is known to work, and the evidence here is a few hundred labels.
    """
    if not new.get("n") or new.get("precision") is None or parent.get("precision") is None:
        return {
            "activate": False,
            "reason": "no holdout labels where both configs decide anything; nothing to compare",
        }

    def gap(key: str, better_up: bool) -> float:
        a, b = new.get(key), parent.get(key)
        if a is None or b is None:
            return 0.0
        return float(a - b) if better_up else float(b - a)

    precision, recall = gap("precision", True), gap("recall", True)
    missed, review = gap("missed", False), gap("review_share", False)
    floor = min(float(parent["precision"]), target_precision) - TOLERANCE
    if float(new["precision"]) < floor:
        return {
            "activate": False,
            "reason": f"holdout precision falls from {parent['precision']:.3f} to {new['precision']:.3f}",
        }
    if missed < -MISSED_TOLERANCE:
        return {
            "activate": False,
            "reason": f"it would miss more true matches ({parent['missed']:.3f} -> {new['missed']:.3f})",
        }
    if review < -TOLERANCE:
        return {
            "activate": False,
            "reason": (
                f"it sends {new['review_share']:.1%} of records to review against "
                f"{parent['review_share']:.1%} - thin labels cannot certify the thresholds it wants"
            ),
        }
    if max(precision, recall, missed, review) <= TOLERANCE:
        return {"activate": False, "reason": "no measurable difference on the holdout"}
    gains = []
    if precision > TOLERANCE:
        gains.append(f"precision {parent['precision']:.3f} -> {new['precision']:.3f}")
    if recall > TOLERANCE:
        gains.append(f"recall {parent['recall']:.3f} -> {new['recall']:.3f}")
    if review > TOLERANCE:
        gains.append(f"review {parent['review_share']:.1%} -> {new['review_share']:.1%}")
    return {"activate": True, "reason": "; ".join(gains)}


def label_counts(labels: Sequence[Label]) -> dict[str, int]:
    counts: dict[str, int] = {
        "total": len(labels),
        "positives": sum(x.label for x in labels),
        "negatives": sum(1 - x.label for x in labels),
    }
    for source in SOURCES:
        counts[source] = sum(1 for x in labels if x.source == source)
    for kind in ModelKind:
        counts[str(kind)] = sum(1 for x in labels if x.kind is kind)
    return counts


def check_enough(labels: Sequence[Label], min_labels: int) -> None:
    """Refuse below the minimum, or with too few of either class to learn from."""
    counts = label_counts(labels)
    floor = max(3, math.ceil(min_labels / 10))
    if counts["total"] < min_labels:
        raise NotEnoughLabelsError(
            f"{counts['total']} labelled pairs; a retune needs at least {min_labels}",
            counts,
        )
    if counts["positives"] < floor or counts["negatives"] < floor:
        raise NotEnoughLabelsError(
            f"{counts['positives']} true and {counts['negatives']} false matches labelled; "
            f"a retune needs at least {floor} of each",
            counts,
        )


def retune(
    parent: ScoringConfig,
    patterns: Mapping[ModelKind, PatternCounts],
    labels: Sequence[Label],
    *,
    config_id: str,
    seed: int,
    target_precision: float,
    target_recall: float = 0.99,
    min_labels: int = 100,
    holdout_fraction: float = DEFAULT_HOLDOUT,
    restarts: int = DEFAULT_RESTARTS,
    min_per_kind: int = MIN_PER_KIND,
    label_noise: float = 0.0,
    population: Sequence[Sequence[tuple[ModelKind, ComparisonVector]]] | None = None,
) -> RetuneResult:
    """Fit a new config from `parent`, a pair tally and reviewer labels.

    `population` is the run's records, each as its candidates' (model, vector)
    pairs; with it, thresholds are placed on the whole run (step 4).
    """
    check_enough(labels, min_labels)
    notes: list[str] = []
    bundles: dict[ModelKind, ModelBundle] = {}
    per_kind: dict[str, Any] = {}
    all_holdout: list[Label] = []

    for kind, parent_bundle in parent.bundles.items():
        mine = [x for x in labels if x.kind is kind]
        fit_split = [x for x in mine if not in_holdout(x.key, seed, holdout_fraction)]
        holdout = [x for x in mine if in_holdout(x.key, seed, holdout_fraction)]
        positives = sum(x.label for x in fit_split)
        report: dict[str, Any] = {
            "labels": len(mine),
            "fit": len(fit_split),
            "holdout": len(holdout),
        }
        data = patterns.get(kind)
        enough = (
            len(mine) >= min_per_kind
            and 0 < positives < len(fit_split)
            and data is not None
            and data.total > 0
        )
        if not enough:
            bundles[kind] = parent_bundle
            report["refit"] = False
            report["reason"] = (
                f"{len(mine)} labels (both classes needed in the fit split, and at least "
                f"{min_per_kind}); the parent's {kind} model is kept unchanged"
            )
            notes.append(f"{kind}: {report['reason']}")
            per_kind[str(kind)] = report
            all_holdout.extend(holdout)
            continue

        assert data is not None
        clamped = ClampedPatterns.from_labels(kind, ((x.vector, x.label) for x in fit_split))
        unlabelled = _without(data, [x for x in fit_split if x.in_tally])
        try:
            model = fit_em(
                unlabelled,
                seed=seed,
                restarts=restarts,
                clamped=clamped,
                init=parent_bundle.model,
                label_noise=label_noise,
            )
        except DegenerateFitError as exc:
            bundles[kind] = parent_bundle
            report["refit"] = False
            report["reason"] = f"semi-supervised EM failed ({exc}); the parent's model is kept"
            notes.append(f"{kind}: {report['reason']}")
            per_kind[str(kind)] = report
            all_holdout.extend(holdout)
            continue

        # Calibrate on the labels, at their weights. `voluntary` reviews of
        # auto-rejects carry an unknown inclusion probability, so they get no
        # weight here (see the module docstring); `calibrate` splits by the same
        # hash, so its holdout is this function's holdout.
        usable = [x for x in mine if x.source != "voluntary"]
        scored = [model.score(x.vector) for x in usable]
        result = calibrate(
            keys=[x.key for x in usable],
            weights=[s.match_weight for s in scored],
            labels=[x.label for x in usable],
            seed=seed,
            raw_probabilities=[s.posterior for s in scored],
            holdout_fraction=holdout_fraction,
            target_precision=target_precision,
            target_recall=target_recall,
            resolution=DEFAULT_RESOLUTION,
            sample_weight=[x.weight for x in usable],
            label_noise=label_noise,
        )
        notes.extend(f"{kind}: {n}" for n in result.notes)
        bundles[kind] = ModelBundle(
            model=model,
            calibrator=result.calibrator,
            thresholds=result.thresholds,
            calibration=result.as_dict(),
        )
        report.update(
            refit=True,
            clamped=clamped.total,
            unlabelled_pairs=unlabelled.total,
            lam=round(model.lam, 6),
            parent_lam=round(parent_bundle.model.lam, 6),
            iterations=model.iterations,
            ece_before=round(result.before.ece, 6),
            ece_after=round(result.after.ece, 6),
            thresholds_from="labels",
            t_auto_accept=round(result.thresholds.t_auto_accept, 6),
            t_auto_reject=round(result.thresholds.t_auto_reject, 6),
            precision_target_met=result.thresholds.precision_target_met,
        )
        per_kind[str(kind)] = report
        all_holdout.extend(holdout)

    refit = [k for k in bundles if per_kind[str(k)].get("refit")]
    if population and refit:
        # Each record's confidence is its best candidate's, each candidate
        # under its own model's new bundle - the same rule the scorer ranks by.
        by_kind: dict[ModelKind, list[float]] = {k: [] for k in bundles}
        for candidates in population:
            best: tuple[ModelKind, float] | None = None
            for kind, vector in candidates:
                bundle = bundles[kind]
                conf = bundle.calibrator.apply(bundle.model.match_weight(vector))
                if best is None or conf > best[1]:
                    best = (kind, conf)
            if best is not None:
                by_kind[best[0]].append(best[1])
        for kind in refit:
            if not by_kind[kind]:
                continue
            placed = expected_thresholds(by_kind[kind], target_precision, target_recall)
            bundles[kind] = replace(bundles[kind], thresholds=placed)
            per_kind[str(kind)].update(
                thresholds_from="population",
                population=len(by_kind[kind]),
                label_t_auto_accept=per_kind[str(kind)]["t_auto_accept"],
                label_t_auto_reject=per_kind[str(kind)]["t_auto_reject"],
                t_auto_accept=round(placed.t_auto_accept, 6),
                t_auto_reject=round(placed.t_auto_reject, 6),
                precision_target_met=placed.precision_target_met,
                expected_precision=round(placed.achieved_precision, 6),
                expected_recall=round(placed.achieved_recall, 6),
                grey_band_fraction=round(placed.grey_band_fraction, 6),
            )

    for kind in refit:
        scored_holdout = [
            x for x in all_holdout if x.kind is kind and x.source != "voluntary"
        ]
        per_kind[str(kind)]["judged"] = {
            "new": judge(bundles[kind], scored_holdout, label_noise),
            "parent": judge(parent.bundles[kind], scored_holdout, label_noise),
        }

    judged = [x for x in all_holdout if x.source != "voluntary"]
    config = ScoringConfig(
        config_id=config_id,
        fitted_at=now_iso(),
        fitted_from="semi_supervised",
        engine_version=__version__,
        seed=seed,
        top_k=parent.top_k,
        margin_delta=parent.margin_delta,
        bundles=bundles,
        dataset={
            **{k: v for k, v in parent.dataset.items() if k != "retune"},
            "retune": {"parent": parent.config_id, "labels": label_counts(labels)},
        },
        notes=notes,
    )
    new_engine = config.engine()
    parent_engine = parent.engine()
    overall = {
        "new": judge_engine(new_engine, judged, label_noise),
        "parent": judge_engine(parent_engine, judged, label_noise),
    }
    metrics = {
        "parent": parent.config_id,
        "labels": label_counts(labels),
        "holdout_fraction": holdout_fraction,
        "target_precision": target_precision,
        "label_noise": label_noise,
        "models": per_kind,
        "holdout": overall,
        "recommendation": recommend(overall["new"], overall["parent"], target_precision),
    }
    return RetuneResult(config=config, metrics=metrics, notes=notes)


def _without(data: PatternCounts, labelled: Sequence[Label]) -> PatternCounts:
    """The tally with the labelled pairs taken out, so none is counted twice."""
    tally: Counter[ComparisonVector] = Counter(dict(zip(data.patterns, data.counts, strict=True)))
    for x in labelled:
        if tally[x.vector] > 0:
            tally[x.vector] -= 1
    kept = sorted(v for v, n in tally.items() if n > 0)
    return PatternCounts(data.kind, tuple(kept), tuple(tally[v] for v in kept))


__all__ = [
    "DEFAULT_HOLDOUT",
    "Label",
    "NotEnoughLabelsError",
    "RetuneResult",
    "check_enough",
    "judge",
    "judge_engine",
    "label_counts",
    "recommend",
    "retune",
    "vector_from_levels",
]
