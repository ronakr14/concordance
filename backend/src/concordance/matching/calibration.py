"""Calibration: making the confidence number mean what it says.

A Fellegi-Sunter posterior is a ranking score. It orders pairs correctly and it
is *not* a probability: the conditional-independence assumption is false - name,
address and ZIP are correlated - so the evidence is double-counted and the
posteriors pile up against 0 and 1. A raw score of 0.95 might be right 99.8% of
the time or 82% of the time, and until it is measured nobody knows which.

That matters here more than it usually does, because the entire threshold
story rests on it. "Auto-accept above the confidence where precision is 99%" is
a business decision only if the confidence is a real probability. Otherwise it
is a number that happens to sort well.

So: split the labelled data, measure the raw posteriors on the holdout
(reliability curve over ten bins, Expected Calibration Error, Brier score), fit
an isotonic regression on the fit split, and measure again. Both sets of
numbers are kept - the before-and-after pair is the evidence that the step did
something, and it is the chart worth showing.

**The calibrator's input is the match weight, not the posterior.** This is not
a detail. `posterior = sigmoid(w * ln2 + logit(lambda))` saturates: a pair at
weight 43 and a pair at weight 37 both come back as 1.0 to within a rounding
error, and the difference between them survives only in the last few bits of a
float. Fitting isotonic on that produces knots spaced 1e-13 apart and a
calibrated confidence that is amplified floating-point noise. The match weight
is the same ranking on a scale with room in it - roughly -60 to +80 log2 units
- so the fitted map is stable and the reliability diagram means something. The
posterior is still computed and reported, because it is the quantity the
Fellegi-Sunter model actually defines; it is simply not what gets calibrated.

Isotonic regression is implemented here rather than imported. Pool-adjacent-
violators is about thirty lines, and owning it means the calibrator serializes
to a list of knots that any language can apply, instead of to a pickle that
only this version of this library can load. Applying a calibrator at inference
is then a pure function of stored numbers, with no refit and no dependency.

**Weights** are optional everywhere and change nothing when omitted. They exist
for reviewer labels (Stage 9), which are not a uniform sample: every accepted
and grey-band result reaches a reviewer, but only a small random audit of the
auto-rejects does. A label drawn with probability pi stands for 1/pi records
like it, and the precision a threshold achieves is a statement about records,
not about labels - so the fit, the reliability metrics and the threshold search
all count each label at its weight.

**Labels may be fractional.** A reviewer who is wrong at rate epsilon produces
labels whose sums are biased: a set of true matches reads as 1 - epsilon of
them. `denoise` turns each verdict into (y - epsilon) / (1 - 2 epsilon), whose
sum over any set is an unbiased estimate of the true count, so precision,
recall and the isotonic fit all come out in true-label terms. Individual values
fall slightly outside [0, 1]; the fitted calibrator is clipped back into it.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BINS = 10
DEFAULT_HOLDOUT = 0.4
# Below this, "precision >= 0.99 at every threshold" is an artefact of having
# nothing to get wrong, not a property of the model.
MIN_HOLDOUT_NEGATIVES = 20
# Quantization of the match weight before isotonic pooling, in log2 units. Half
# a unit is a likelihood ratio of sqrt(2): two pairs whose total evidence
# differs by less than that are not meaningfully distinguishable, so they
# belong in one block. Chosen as a round interpretable interval rather than
# searched - searching it on the holdout would be tuning on the measurement.
DEFAULT_RESOLUTION = 0.5


# --------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------


def in_holdout(key: str, seed: int, holdout_fraction: float = DEFAULT_HOLDOUT) -> bool:
    """Deterministic per-key split.

    Hashing the key rather than shuffling a list means the split survives a
    record being added, removed or reordered: the same record lands on the same
    side of the line in every run, at every corruption level, forever. A
    shuffled split would quietly reshuffle the holdout each time the dataset
    regenerated and make two evaluation runs incomparable.
    """
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < holdout_fraction


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """One bar of the reliability diagram."""

    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_frequency: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "lower": round(self.lower, 4),
            "upper": round(self.upper, 4),
            "count": self.count,
            "mean_predicted": round(self.mean_predicted, 6),
            "observed_frequency": round(self.observed_frequency, 6),
        }


@dataclass(frozen=True, slots=True)
class CalibrationMetrics:
    """How well a set of confidences matched reality."""

    n: int
    ece: float
    mce: float
    brier: float
    bins: tuple[ReliabilityBin, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "ece": round(self.ece, 6),
            "mce": round(self.mce, 6),
            "brier": round(self.brier, 6),
            "bins": [b.as_dict() for b in self.bins],
        }


def reliability(
    predicted: Sequence[float],
    actual: Sequence[float],
    bins: int = DEFAULT_BINS,
    weights: Sequence[float] | None = None,
) -> CalibrationMetrics:
    """Reliability bins, Expected Calibration Error and Brier score.

    ECE is the count-weighted mean gap between predicted confidence and observed
    frequency; MCE is the worst single bin, which is what catches a calibrator
    that is excellent on average and badly wrong exactly where the thresholds
    sit.
    """
    n = len(predicted)
    if n != len(actual):
        raise ValueError("predicted and actual must be the same length")
    if n == 0:
        return CalibrationMetrics(0, 0.0, 0.0, 0.0, ())

    w = list(weights) if weights is not None else [1.0] * n
    if len(w) != n:
        raise ValueError("weights must match predicted in length")
    total_weight = sum(w) or 1.0

    edges = [i / bins for i in range(bins + 1)]
    sums = [0.0] * bins
    hits = [0.0] * bins
    mass = [0.0] * bins
    counts = [0] * bins
    for p, y, wi in zip(predicted, actual, w, strict=True):
        # The top edge belongs to the last bin rather than opening an 11th.
        index = min(max(int(p * bins), 0), bins - 1)
        sums[index] += wi * p
        hits[index] += wi * y
        mass[index] += wi
        counts[index] += 1

    out: list[ReliabilityBin] = []
    ece = 0.0
    mce = 0.0
    for i in range(bins):
        if not counts[i] or mass[i] <= 0:
            out.append(ReliabilityBin(edges[i], edges[i + 1], counts[i], 0.0, 0.0))
            continue
        mean_p = sums[i] / mass[i]
        observed = hits[i] / mass[i]
        gap = abs(mean_p - observed)
        ece += (mass[i] / total_weight) * gap
        mce = max(mce, gap)
        out.append(ReliabilityBin(edges[i], edges[i + 1], counts[i], mean_p, observed))

    brier = (
        sum(wi * (p - y) ** 2 for p, y, wi in zip(predicted, actual, w, strict=True))
        / total_weight
    )
    return CalibrationMetrics(n, ece, mce, brier, tuple(out))


# --------------------------------------------------------------------------
# isotonic regression
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IsotonicCalibrator:
    """A monotone step function, stored as knots and applied by interpolation.

    `x` is non-decreasing and `y` is non-decreasing; between two knots the value
    is interpolated linearly, outside them it is clamped. That is the whole
    model: applying it is arithmetic over stored numbers, so inference never
    refits and never needs the fitting code at all.
    """

    x: tuple[float, ...]
    y: tuple[float, ...]

    def __call__(self, p: float) -> float:
        return self.apply(p)

    def apply(self, p: float) -> float:
        xs, ys = self.x, self.y
        if not xs:
            return p
        if p <= xs[0]:
            return ys[0]
        if p >= xs[-1]:
            return ys[-1]
        lo, hi = 0, len(xs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xs[mid] <= p:
                lo = mid
            else:
                hi = mid
        span = xs[hi] - xs[lo]
        if span <= 0:
            return ys[hi]
        return ys[lo] + (ys[hi] - ys[lo]) * (p - xs[lo]) / span

    def as_dict(self) -> dict[str, Any]:
        return {"kind": "isotonic", "x": list(self.x), "y": list(self.y)}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> IsotonicCalibrator:
        return cls(tuple(float(v) for v in payload["x"]), tuple(float(v) for v in payload["y"]))

    @classmethod
    def identity(cls) -> IsotonicCalibrator:
        """The calibrator that changes nothing on [0, 1] - the uncalibrated baseline."""
        return cls((0.0, 1.0), (0.0, 1.0))

    @classmethod
    def passthrough(cls) -> IsotonicCalibrator:
        """Identity over the whole match-weight range.

        Used by the provisional engine during fitting, where the calibrator has
        not been fitted yet but candidates still have to be ranked. Returning
        the weight unchanged keeps the ranking exact; clamping to [0, 1] would
        flatten every strong candidate onto the same value.
        """
        return cls((-1e9, 1e9), (-1e9, 1e9))


def fit_isotonic(
    predicted: Sequence[float],
    actual: Sequence[float],
    resolution: float | None = None,
    weights: Sequence[float] | None = None,
) -> IsotonicCalibrator:
    """Pool-adjacent-violators, on points sorted by predicted score.

    The fit is a step function that is as close as possible to the observed
    labels while never decreasing - which is exactly the constraint wanted here,
    because a higher score must never mean a lower probability of being right.

    `resolution` quantizes the input before pooling, and it is the difference
    between a calibrator and an overfitted one. Unregularized PAVA will place a
    near-vertical step wherever the fit split happens to flip, and a holdout
    record landing on the wrong side of that step gets a confidence that is
    confidently wrong. Rounding the match weight to a fraction of a log2 unit
    first forces genuinely indistinguishable pairs into one block, which is
    what they are.
    """
    if not predicted:
        return IsotonicCalibrator.identity()

    if resolution:
        predicted = [round(p / resolution) * resolution for p in predicted]
    w = list(weights) if weights is not None else [1.0] * len(predicted)
    points = sorted(zip(predicted, actual, w, strict=True), key=lambda t: (t[0], t[1]))
    # Blocks of (weighted sum of y, weight, x at the right edge). Adjacent
    # blocks that violate monotonicity are pooled until none do.
    blocks: list[list[float]] = []
    for p, y, wi in points:
        if wi <= 0:
            continue
        blocks.append([float(y) * wi, float(wi), float(p)])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            total, weight, right = blocks.pop()
            blocks[-1][0] += total
            blocks[-1][1] += weight
            blocks[-1][2] = right

    if not blocks:
        return IsotonicCalibrator.identity()
    # De-noised labels can pool to a block mean a hair outside [0, 1]; a
    # probability cannot. Clipping a non-decreasing sequence keeps it so.
    for block in blocks:
        block[0] = min(max(block[0] / block[1], 0.0), 1.0) * block[1]
    xs: list[float] = []
    ys: list[float] = []
    left = points[0][0]
    for total, weight, right in blocks:
        value = total / weight
        # Each block spans an interval of scores; emitting both ends keeps the
        # interpolation faithful to the step rather than smoothing it away.
        if not xs or left > xs[-1]:
            xs.append(left)
            ys.append(value)
        if right > xs[-1]:
            xs.append(right)
            ys.append(value)
        else:
            ys[-1] = value
        left = right
    if len(xs) < 2:
        xs = [0.0, 1.0]
        ys = [ys[0] if ys else 0.5] * 2
    return IsotonicCalibrator(tuple(xs), tuple(ys))


# --------------------------------------------------------------------------
# thresholds
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The two numbers that turn a confidence into a decision."""

    t_auto_accept: float
    t_auto_reject: float
    target_precision: float
    target_recall: float
    achieved_precision: float
    achieved_recall: float
    grey_band_fraction: float
    n_holdout: int
    precision_target_met: bool = True
    recall_target_met: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "t_auto_accept": round(self.t_auto_accept, 6),
            "t_auto_reject": round(self.t_auto_reject, 6),
            "target_precision": self.target_precision,
            "target_recall": self.target_recall,
            "achieved_precision": round(self.achieved_precision, 6),
            "achieved_recall": round(self.achieved_recall, 6),
            "grey_band_fraction": round(self.grey_band_fraction, 6),
            "n_holdout": self.n_holdout,
            "precision_target_met": self.precision_target_met,
            "recall_target_met": self.recall_target_met,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Thresholds:
        return cls(
            t_auto_accept=float(payload["t_auto_accept"]),
            t_auto_reject=float(payload["t_auto_reject"]),
            target_precision=float(payload.get("target_precision", 0.99)),
            target_recall=float(payload.get("target_recall", 0.99)),
            achieved_precision=float(payload.get("achieved_precision", 0.0)),
            achieved_recall=float(payload.get("achieved_recall", 0.0)),
            grey_band_fraction=float(payload.get("grey_band_fraction", 0.0)),
            n_holdout=int(payload.get("n_holdout", 0)),
            precision_target_met=bool(payload.get("precision_target_met", True)),
            recall_target_met=bool(payload.get("recall_target_met", True)),
        )


def choose_thresholds(
    confidence: Sequence[float],
    actual: Sequence[float],
    target_precision: float = 0.99,
    target_recall: float = 0.99,
    weights: Sequence[float] | None = None,
) -> Thresholds:
    """Pick the accept and reject thresholds from holdout performance.

    `t_auto_accept` is the *lowest* confidence at which auto-accepting
    everything above it still meets the precision target - lowest, because any
    higher threshold meets the target too while sending more work to a human
    for no gain.

    `t_auto_reject` is the symmetric choice on recall: the *highest* confidence
    at which auto-rejecting everything below it still keeps the recall target,
    so the band is as narrow as the targets allow. Everything between is the
    grey band, and its width as a share of volume is the number that decides
    what the LLM stage costs.
    """
    n = len(confidence)
    if n == 0:
        return Thresholds(1.0, 0.0, target_precision, target_recall, 0.0, 0.0, 1.0, 0, False, False)

    w = list(weights) if weights is not None else [1.0] * n
    points = sorted(zip(confidence, actual, w, strict=True), key=lambda t: -t[0])
    positives = sum(wi for y, wi in zip(actual, w, strict=True) if y)

    # Accept threshold: sweep down the ranking, keeping the last cut that still
    # met the precision target.
    tp = 0.0
    fp = 0.0
    accept = 1.0
    achieved_precision = 0.0
    precision_met = False
    for i, (score, label, wi) in enumerate(points):
        tp += wi * label
        fp += wi * (1 - label)
        # Only cut between distinct scores; a threshold inside a tie is not a
        # threshold that can be applied.
        if i + 1 < n and points[i + 1][0] == score:
            continue
        if tp + fp <= 0:
            continue
        precision = tp / (tp + fp)
        if precision >= target_precision:
            accept = score
            achieved_precision = precision
            precision_met = True
        elif not precision_met:
            # Not met anywhere yet. Keep the best precision seen so the report
            # says how far short the model fell rather than claiming 1.0.
            achieved_precision = max(achieved_precision, precision)

    # Reject threshold: the scorer rejects on `confidence < t_auto_reject`, so
    # the threshold is the distinct score *above* the last one being discarded.
    # Setting it to the discarded score itself leaves that whole group sitting
    # in the grey band - which, when a model separates cleanly and its
    # negatives all land on one value, is every negative it had.
    distinct = sorted({score for score, _, _ in points})
    positives_at_or_below: dict[float, float] = {}
    running = 0.0
    for score, label, wi in sorted(zip(confidence, actual, w, strict=True), key=lambda t: t[0]):
        running += wi * label
        positives_at_or_below[score] = running

    reject = distinct[0]
    achieved_recall = 1.0
    recall_met = positives == 0
    for i, score in enumerate(distinct):
        recall = (positives - positives_at_or_below[score]) / positives if positives else 1.0
        if recall < target_recall:
            break
        # Everything at or below `score` is discarded, so the cut sits at the
        # next distinct value up.
        reject = distinct[i + 1] if i + 1 < len(distinct) else score
        achieved_recall = recall
        recall_met = True

    if reject > accept:
        # The targets are jointly unreachable on this holdout. Collapsing the
        # band silently would hide that, so the band is emptied at the accept
        # threshold and the achieved numbers below say why.
        reject = accept

    grey = sum(wi for c, wi in zip(confidence, w, strict=True) if reject <= c < accept) / (
        sum(w) or 1.0
    )
    return Thresholds(
        t_auto_accept=accept,
        t_auto_reject=reject,
        target_precision=target_precision,
        target_recall=target_recall,
        achieved_precision=min(achieved_precision, 1.0),
        achieved_recall=min(achieved_recall, 1.0) if positives else 0.0,
        grey_band_fraction=grey,
        n_holdout=n,
        precision_target_met=precision_met,
        recall_target_met=recall_met,
    )


# --------------------------------------------------------------------------
# the whole step
# --------------------------------------------------------------------------


@dataclass
class CalibrationResult:
    """Everything the calibration step produced, before and after."""

    calibrator: IsotonicCalibrator
    thresholds: Thresholds
    before: CalibrationMetrics
    after: CalibrationMetrics
    n_fit: int
    n_holdout: int
    holdout_fraction: float
    seed: int
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calibrator": self.calibrator.as_dict(),
            "thresholds": self.thresholds.as_dict(),
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
            "n_fit": self.n_fit,
            "n_holdout": self.n_holdout,
            "holdout_fraction": self.holdout_fraction,
            "seed": self.seed,
            "notes": list(self.notes),
        }


def calibrate(
    keys: Sequence[str],
    weights: Sequence[float],
    labels: Sequence[float],
    seed: int,
    raw_probabilities: Sequence[float] | None = None,
    holdout_fraction: float = DEFAULT_HOLDOUT,
    bins: int = DEFAULT_BINS,
    target_precision: float = 0.99,
    target_recall: float = 0.99,
    resolution: float | None = DEFAULT_RESOLUTION,
    sample_weight: Sequence[float] | None = None,
    label_noise: float = 0.0,
) -> CalibrationResult:
    """Split, fit isotonic on the fit half, measure the holdout, pick thresholds.

    The calibrator is fitted on the fit split only and every reported number
    comes from the holdout, so nothing in the report was measured on data the
    calibrator had seen. The thresholds come from the holdout too, for the same
    reason: a threshold chosen on the fit split is a threshold chosen on the
    data it was tuned to.

    `weights` are the match weights the calibrator is fitted on;
    `raw_probabilities` are the model's own uncalibrated posteriors for the same
    records, used only to compute the "before" metrics so the report can show
    what calibration changed. Passing no probabilities treats `weights` as
    already being probabilities, which is what the tests do.

    Both splits hold one point per record: the best candidate for that record. Widening them with runner-up candidates was tried and is wrong -
    the runners-up are almost all negatives, so the fitted map becomes
    P(link | confidence, any rank) when what a threshold needs is
    P(link | confidence, rank 1). Measured on this dataset it made holdout ECE
    almost three times worse.
    """
    raw = list(raw_probabilities) if raw_probabilities is not None else list(weights)
    label_weights = list(sample_weight) if sample_weight is not None else [1.0] * len(keys)
    fit_p: list[float] = []
    fit_y: list[float] = []
    fit_w: list[float] = []
    hold_p: list[float] = []
    hold_y: list[float] = []
    hold_w: list[float] = []
    hold_raw: list[float] = []
    for key, w, y, r, wi in zip(keys, weights, labels, raw, label_weights, strict=True):
        if in_holdout(key, seed, holdout_fraction):
            hold_p.append(w)
            hold_y.append(y)
            hold_w.append(wi)
            hold_raw.append(r)
        else:
            fit_p.append(w)
            fit_y.append(y)
            fit_w.append(wi)

    notes: list[str] = []
    if not hold_p:
        hold_raw = list(fit_p) if raw_probabilities is None else hold_raw
        # Small fixtures and tiny organization samples can land entirely on one
        # side. Reporting on the fit split is wrong but silence is worse, so it
        # is done and said.
        hold_p, hold_y, hold_w = fit_p, fit_y, fit_w
        hold_raw = hold_raw or list(fit_p)
        notes.append("holdout empty; metrics computed on the fit split")

    # Labels are counted de-noised from here on, so every rate - the fitted
    # curve included - estimates the true one rather than the observed one.
    weighted = sample_weight is not None
    fit_y = denoise_soft(fit_y, label_noise)
    hold_y = denoise_soft(hold_y, label_noise)
    calibrator = (
        fit_isotonic(fit_p, fit_y, resolution, fit_w if weighted else None)
        if fit_p
        else IsotonicCalibrator.identity()
    )
    before = reliability(hold_raw, hold_y, bins, hold_w if weighted else None)
    calibrated = [calibrator.apply(p) for p in hold_p]
    after = reliability(calibrated, hold_y, bins, hold_w if weighted else None)
    if after.ece > before.ece:
        notes.append(
            f"isotonic did not improve ECE on holdout ({before.ece:.4f} -> {after.ece:.4f})"
        )

    negatives = len(hold_y) - sum(hold_y)
    if negatives < MIN_HOLDOUT_NEGATIVES:
        # With no negatives every threshold has perfect precision, so the
        # selection rule returns the lowest confidence in the set and every
        # record auto-accepts. The number is real; what it is not is evidence.
        notes.append(
            f"only {negatives} negative(s) in the holdout: the accept threshold is not "
            "identifiable from this data and should not be read as a precision guarantee"
        )

    thresholds = choose_thresholds(
        calibrated, hold_y, target_precision, target_recall, hold_w if weighted else None
    )
    if not thresholds.precision_target_met:
        notes.append(
            f"precision target {target_precision} unreachable on holdout; "
            f"best precision {thresholds.achieved_precision:.4f}, nothing auto-accepts"
        )
    return CalibrationResult(
        calibrator=calibrator,
        thresholds=thresholds,
        before=before,
        after=after,
        n_fit=len(fit_p),
        n_holdout=len(hold_p),
        holdout_fraction=holdout_fraction,
        seed=seed,
        notes=notes,
    )


def expected_thresholds(
    confidence: Sequence[float],
    target_precision: float = 0.99,
    target_recall: float = 0.99,
) -> Thresholds:
    """Thresholds from a population's calibrated confidences, with no labels at all.

    If the confidences are calibrated, the precision of auto-accepting every
    record at or above `t` is the mean confidence of those records, and the
    true matches below `t` number the sum of their confidences. So both
    thresholds can be chosen on every record a run scored - thousands of them -
    instead of on the few hundred labels a retune has, where a 99% target is
    decided by whether one or two negatives happen to fall in the holdout. The
    labels still decide the calibration; this only decides where to cut it.

    `accept` is the lowest confidence whose population above it still averages
    `target_precision`; `reject` the highest whose population below it holds no
    more than `1 - target_recall` of the expected matches.
    """
    n = len(confidence)
    if n == 0:
        return Thresholds(1.0, 0.0, target_precision, target_recall, 0.0, 0.0, 1.0, 0, False, False)

    descending = sorted(confidence, reverse=True)
    accept = 1.0
    achieved_precision = 0.0
    precision_met = False
    running = 0.0
    for i, c in enumerate(descending):
        running += c
        if i + 1 < n and descending[i + 1] == c:
            continue
        mean = running / (i + 1)
        # A running mean of a descending sequence only falls, so the first
        # miss is the last chance.
        if mean < target_precision:
            if not precision_met:
                achieved_precision = mean
            break
        accept, achieved_precision, precision_met = c, mean, True

    expected = sum(confidence)
    ascending = descending[::-1]
    distinct = sorted(set(ascending))
    below: dict[float, float] = {}
    running = 0.0
    for c in ascending:
        running += c
        below[c] = running
    reject = distinct[0]
    achieved_recall = 1.0
    recall_met = expected <= 0
    for i, c in enumerate(distinct):
        recall = (expected - below[c]) / expected if expected > 0 else 1.0
        if recall < target_recall:
            break
        reject = distinct[i + 1] if i + 1 < len(distinct) else c
        achieved_recall = recall
        recall_met = True
    if reject > accept:
        reject = accept

    grey = sum(1 for c in confidence if reject <= c < accept) / n
    return Thresholds(
        t_auto_accept=accept,
        t_auto_reject=reject,
        target_precision=target_precision,
        target_recall=target_recall,
        achieved_precision=min(achieved_precision, 1.0),
        achieved_recall=min(achieved_recall, 1.0),
        grey_band_fraction=grey,
        n_holdout=n,
        precision_target_met=precision_met,
        recall_target_met=recall_met,
    )


def denoise_soft(labels: Sequence[float], noise: float) -> list[float]:
    """`denoise` for labels already given as floats; unchanged at noise 0."""
    return list(labels) if noise == 0 else [(y - noise) / (1.0 - 2.0 * noise) for y in labels]


def denoise(labels: Sequence[int], noise: float) -> list[float]:
    """Unbiased per-label estimates of the true label, given a reviewer error rate.

    With symmetric error rate epsilon, E[observed] = epsilon + (1 - 2 epsilon) * true,
    so (observed - epsilon) / (1 - 2 epsilon) has expectation equal to the true
    label. At epsilon = 0 the labels come back unchanged.
    """
    if not 0.0 <= noise < 0.5:
        raise ValueError(f"noise must be in [0, 0.5), got {noise}")
    if noise == 0:
        return [float(y) for y in labels]
    return [(y - noise) / (1.0 - 2.0 * noise) for y in labels]


def logistic_floor(value: float) -> float:
    """Clamp into the open unit interval - a 0 or a 1 breaks the log-odds."""
    return min(max(value, 1e-12), 1.0 - 1e-12)


def log_loss(predicted: Sequence[float], actual: Sequence[int]) -> float:
    """Mean negative log-likelihood. Reported beside Brier as a sanity check."""
    if not predicted:
        return 0.0
    total = 0.0
    for p, y in zip(predicted, actual, strict=True):
        q = logistic_floor(p)
        total += -(y * math.log(q) + (1 - y) * math.log(1.0 - q))
    return total / len(predicted)
