"""Fellegi-Sunter record linkage, with the parameters learned by EM.

The model. For each field *i* and agreement level *l*:

    m[i][l] = P(level = l | the pair is a match)
    u[i][l] = P(level = l | the pair is a non-match)

and a mixing proportion lambda = P(a candidate pair is a match). Under the
conditional-independence assumption the log-likelihood ratio for a pair whose
observed levels are l(i) is a plain sum, which is the whole appeal:

    w = sum_i log2( m[i][l(i)] / u[i][l(i)] )
    posterior = 1 / (1 + exp(-(w * ln2 + logit(lambda))))

Nothing here is labelled. EM fits m, u and lambda from the candidate pairs
themselves, treating "is this pair a match" as the latent variable. That is the
argument for this approach over hand-tuned weights, and it is worth stating
plainly: the fit *learns from the data* that agreeing on a rare surname is
stronger evidence than agreeing on a common one, and that a missing date of
birth is not the same event as a contradicting one. Nobody has to guess a
number, and when the data changes the weights change with it.

Guard rails, each of which exists because the unguarded version fails:

- **Smoothing toward the observed marginal, in proportion to component mass.**
  Plain Laplace smoothing is a trap here. With lambda near 0.03 the match
  component carries thirty times less mass than the non-match one, so adding
  the same pseudo-count to both inflates `m` about thirty times more than `u`,
  and a level that was never observed at all comes out with a *large positive*
  weight - the model ends up treating "something I have never seen" as strong
  evidence of a match. Smoothing each component toward the overall marginal
  with a pseudo-mass proportional to that component's own mass makes an
  unobserved level score exactly zero, which is what "no evidence" should mean.
- **A floor on u.** Without it a level that is merely rare among non-matches
  drives `m/u` toward infinity and one field silently becomes the whole model.
- **Label-switching correction.** EM has no idea which of its two components is
  the match class; the labels are chosen after the fit by comparing mean
  agreement, not assumed.
- **Degenerate-fit detection.** lambda collapsing to 0 or 1 means the fit found
  one component, not two, and the right response is to fail loudly rather than
  to serve a model whose posteriors are all the same number.
- **Fixed seeded restarts.** The same seed and the same input give byte-identical
  parameters, which is what makes a run replayable at Stage 6.

The fit can also be **warm-started from a small labelled slice**. It is optional
and the default fit does not use it - the whole point of EM here is that it
needs no labels - but where labels do exist the first restart begins from the
tables those labels imply rather than from the generic agreement-shaped prior.
That matters in two places: an organization fit with few pairs, where the
likelihood surface is shallow enough for the starting point to decide the
answer, and the Stage 9 feedback loop, where reviewer decisions accumulate into
exactly such a slice.

**Semi-supervised EM** is how reviewer labels enter a refit (Stage 9). A
labelled pair keeps its place in the data, but its verdict becomes a second
observation of its class. Reviewers are wrong at some rate epsilon, so a
verdict is evidence rather than truth: the pair's responsibility is
P(match | its vector, its verdict), and its likelihood term is the probability
of both. With epsilon at 0 that is a clamp - the responsibility is the label -
and with a few percent it stops one mistaken verdict on a textbook match from
teaching the model that agreement means nothing. The unlabelled pairs are
fitted exactly as before. This is the likelihood of the
data actually observed, so under the one assumption it needs - whether a pair
got reviewed depends only on things the system observed, its scores and a
random audit draw - it is unbiased without any reweighting. The alternative,
adding reviewed pairs to the tables as pseudo-counts, is not: reviewers see the
hard cases, and `u` describes every candidate pair, most of them easy
non-matches. Feeding it hard negatives teaches it that agreement is common
among non-matches, and recall pays for it.

The fit runs over *distinct comparison patterns with counts*, not over pairs.
Two hundred thousand candidate pairs collapse to a few thousand distinct
vectors, so an iteration costs a few thousand operations rather than a few
hundred thousand, and a full fit takes well under a second.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from concordance.matching.comparators import (
    FIELD_NAMES,
    LEVEL_COUNTS,
    ComparisonVector,
    ModelKind,
)

LN2 = math.log(2.0)

DEFAULT_SMOOTHING = 1.0
DEFAULT_U_FLOOR = 1e-6
DEFAULT_M_FLOOR = 1e-6
DEFAULT_MAX_ITER = 200
DEFAULT_TOLERANCE = 1e-8
DEFAULT_RESTARTS = 5
# Outside this band a two-component mixture has not been found, whatever the
# log-likelihood says.
LAMBDA_MIN = 1e-4
LAMBDA_MAX = 1.0 - 1e-4


class DegenerateFitError(RuntimeError):
    """The EM fit collapsed to one component. Never silently downgraded."""


def logit(p: float) -> float:
    p = min(max(p, 1e-12), 1.0 - 1e-12)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    """Overflow-free logistic."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _log_add(a: float, b: float) -> float:
    """log(exp(a) + exp(b)), without leaving log space."""
    if a == -math.inf:
        return b
    if b == -math.inf:
        return a
    hi, lo = (a, b) if a > b else (b, a)
    return hi + math.log1p(math.exp(lo - hi))


# --------------------------------------------------------------------------
# training input
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabelledSlice:
    """A handful of pairs whose answer is already known.

    Optional input to `fit_em`. Small by assumption - a few hundred reviewer
    decisions, not a training set - so it seeds the search rather than
    replacing it.
    """

    vectors: tuple[ComparisonVector, ...]
    labels: tuple[int, ...]

    def __len__(self) -> int:
        return len(self.vectors)

    @property
    def positives(self) -> int:
        return sum(self.labels)


@dataclass(frozen=True, slots=True)
class ClampedPatterns:
    """Labelled comparison vectors, with counts: pairs whose class is known.

    The input to semi-supervised EM. Aggregated like `PatternCounts`, because a
    thousand reviewer labels collapse to a few hundred distinct (vector, label)
    pairs.
    """

    kind: ModelKind
    patterns: tuple[ComparisonVector, ...]
    labels: tuple[int, ...]
    counts: tuple[int, ...]

    @property
    def total(self) -> int:
        return sum(self.counts)

    @property
    def positives(self) -> int:
        return sum(c for c, y in zip(self.counts, self.labels, strict=True) if y)

    @classmethod
    def from_labels(
        cls, kind: ModelKind, labelled: Iterable[tuple[ComparisonVector, int]]
    ) -> ClampedPatterns:
        tally: Counter[tuple[ComparisonVector, int]] = Counter(
            (tuple(v), 1 if y else 0) for v, y in labelled
        )
        ordered = sorted(tally)
        return cls(
            kind,
            tuple(v for v, _ in ordered),
            tuple(y for _, y in ordered),
            tuple(tally[k] for k in ordered),
        )


@dataclass(frozen=True, slots=True)
class PatternCounts:
    """Distinct comparison vectors and how often each was observed.

    EM sees the data only through this, which is why a fit over a quarter of a
    million candidate pairs costs the same as a fit over a few thousand: the
    number of *distinct* vectors is bounded by the level tables, not by the
    number of pairs.
    """

    kind: ModelKind
    patterns: tuple[ComparisonVector, ...]
    counts: tuple[int, ...]

    @property
    def total(self) -> int:
        return sum(self.counts)

    @classmethod
    def from_vectors(cls, kind: ModelKind, vectors: Iterable[ComparisonVector]) -> PatternCounts:
        tally: Counter[ComparisonVector] = Counter(vectors)
        # Sorted so the fit does not depend on dictionary insertion order.
        ordered = sorted(tally)
        return cls(kind, tuple(ordered), tuple(tally[v] for v in ordered))


# --------------------------------------------------------------------------
# the fitted model
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldWeight:
    """One field's contribution to a single pair's score."""

    field: str
    level: int
    level_name: str
    m: float
    u: float
    weight: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "level": self.level,
            "level_name": self.level_name,
            "m": self.m,
            "u": self.u,
            "weight": round(self.weight, 6),
        }


@dataclass(frozen=True, slots=True)
class PairScore:
    """A scored pair, with the per-field breakdown the Investigation UI needs."""

    match_weight: float
    posterior: float
    contributions: tuple[FieldWeight, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_weight": round(self.match_weight, 6),
            "posterior": self.posterior,
            "field_weights": [c.as_dict() for c in self.contributions],
        }


@dataclass
class FellegiSunterModel:
    """Fitted m, u and lambda for one model kind.

    Serializes losslessly to and from the JSON that becomes
    `scoring_configs.params` at Stage 5 - `to_dict` and `from_dict` are exact
    inverses, with no reshaping deferred to the loader.
    """

    kind: ModelKind
    fields: tuple[str, ...]
    m: dict[str, list[float]]
    u: dict[str, list[float]]
    lam: float
    u_floor: float = DEFAULT_U_FLOOR
    smoothing: float = DEFAULT_SMOOTHING
    n_pairs: int = 0
    n_patterns: int = 0
    seed: int = 0
    restarts: int = DEFAULT_RESTARTS
    warm_started: bool = False
    iterations: int = 0
    log_likelihood: float = 0.0
    convergence: list[dict[str, float]] = field(default_factory=list)

    # -- scoring ----------------------------------------------------------
    def weights(self) -> dict[str, list[float]]:
        """`log2(m/u)` per field and level - the table a human should read."""
        return {
            name: [
                math.log2(
                    max(self.m[name][level], DEFAULT_M_FLOOR)
                    / max(self.u[name][level], self.u_floor)
                )
                for level in range(len(self.m[name]))
            ]
            for name in self.fields
        }

    def score(self, vector: ComparisonVector) -> PairScore:
        """Match weight, posterior and the per-field breakdown for one pair."""
        from concordance.matching.comparators import FIELDS_BY_KIND

        specs = FIELDS_BY_KIND[self.kind]
        total = 0.0
        contributions: list[FieldWeight] = []
        for spec, level in zip(specs, vector, strict=True):
            m = max(self.m[spec.name][level], DEFAULT_M_FLOOR)
            u = max(self.u[spec.name][level], self.u_floor)
            w = math.log2(m / u)
            total += w
            contributions.append(FieldWeight(spec.name, level, spec.level_name(level), m, u, w))
        posterior = sigmoid(total * LN2 + logit(self.lam))
        return PairScore(total, posterior, tuple(contributions))

    def match_weight(self, vector: ComparisonVector) -> float:
        return sum(
            math.log2(
                max(self.m[name][level], DEFAULT_M_FLOOR) / max(self.u[name][level], self.u_floor)
            )
            for name, level in zip(self.fields, vector, strict=True)
        )

    # -- serialization ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "fields": list(self.fields),
            "m": {k: list(v) for k, v in self.m.items()},
            "u": {k: list(v) for k, v in self.u.items()},
            "lambda": self.lam,
            "u_floor": self.u_floor,
            "smoothing": self.smoothing,
            "n_pairs": self.n_pairs,
            "n_patterns": self.n_patterns,
            "seed": self.seed,
            "restarts": self.restarts,
            "warm_started": self.warm_started,
            "iterations": self.iterations,
            "log_likelihood": self.log_likelihood,
            "convergence": self.convergence,
            "weights_log2": {k: [round(w, 6) for w in v] for k, v in self.weights().items()},
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FellegiSunterModel:
        return cls(
            kind=ModelKind(payload["kind"]),
            fields=tuple(payload["fields"]),
            m={k: list(v) for k, v in payload["m"].items()},
            u={k: list(v) for k, v in payload["u"].items()},
            lam=float(payload["lambda"]),
            u_floor=float(payload.get("u_floor", DEFAULT_U_FLOOR)),
            smoothing=float(payload.get("smoothing", DEFAULT_SMOOTHING)),
            n_pairs=int(payload.get("n_pairs", 0)),
            n_patterns=int(payload.get("n_patterns", 0)),
            seed=int(payload.get("seed", 0)),
            restarts=int(payload.get("restarts", DEFAULT_RESTARTS)),
            warm_started=bool(payload.get("warm_started", False)),
            iterations=int(payload.get("iterations", 0)),
            log_likelihood=float(payload.get("log_likelihood", 0.0)),
            convergence=[dict(row) for row in payload.get("convergence", [])],
        )


# --------------------------------------------------------------------------
# EM
# --------------------------------------------------------------------------


def _warm_start_tables(
    kind: ModelKind, labelled: LabelledSlice, smoothing: float
) -> tuple[dict[str, list[float]], dict[str, list[float]], float]:
    """m, u and lambda read straight off a labelled slice.

    Smoothed the same way the M-step smooths, so a level the slice happens not
    to contain starts neutral rather than at zero.
    """
    names = FIELD_NAMES[kind]
    sizes = LEVEL_COUNTS[kind]
    m_counts = {name: [0.0] * size for name, size in zip(names, sizes, strict=True)}
    u_counts = {name: [0.0] * size for name, size in zip(names, sizes, strict=True)}
    positives = 0
    for vector, label in zip(labelled.vectors, labelled.labels, strict=True):
        target = m_counts if label else u_counts
        positives += bool(label)
        for name, level in zip(names, vector, strict=True):
            target[name][level] += 1.0

    total = float(len(labelled)) or 1.0
    match_mass = float(positives)
    m: dict[str, list[float]] = {}
    u: dict[str, list[float]] = {}
    for name, size in zip(names, sizes, strict=True):
        marginal = _marginal(m_counts[name], u_counts[name], size)
        m[name] = _smoothed(m_counts[name], smoothing, size, marginal, match_mass, total)
        u[name] = _smoothed(
            u_counts[name], smoothing, size, marginal, total - match_mass, total
        )
    lam = min(max(match_mass / total, 0.01), 0.5)
    return m, u, lam


def _initial_tables(
    kind: ModelKind, rng: random.Random, jitter: float
) -> tuple[dict[str, list[float]], dict[str, list[float]], float]:
    """Start m biased toward agreement and u toward disagreement.

    EM finds a local optimum, so where it starts decides which one. Starting
    from the shape the answer is known to have - matches agree, non-matches do
    not - converges in a handful of iterations and keeps the two components
    distinguishable. The jitter is what makes the restarts explore rather than
    repeat; it is drawn from a seeded generator, so it is reproducible.
    """
    names = FIELD_NAMES[kind]
    sizes = LEVEL_COUNTS[kind]
    m: dict[str, list[float]] = {}
    u: dict[str, list[float]] = {}
    for name, size in zip(names, sizes, strict=True):
        top = size - 1
        m_row = [
            math.exp(2.0 * (level - top) / max(top, 1)) * (1.0 + jitter * rng.uniform(-1, 1))
            for level in range(size)
        ]
        u_row = [
            math.exp(-2.0 * (level - 0) / max(top, 1)) * (1.0 + jitter * rng.uniform(-1, 1))
            for level in range(size)
        ]
        m[name] = _normalize([max(v, 1e-6) for v in m_row])
        u[name] = _normalize([max(v, 1e-6) for v in u_row])
    lam = 0.1 * (1.0 + jitter * rng.uniform(-1, 1))
    return m, u, min(max(lam, 0.01), 0.5)


def _normalize(row: Sequence[float]) -> list[float]:
    total = sum(row)
    return [v / total for v in row] if total else [1.0 / len(row)] * len(row)


def _log_tables(tables: Mapping[str, list[float]], floor: float) -> dict[str, list[float]]:
    return {name: [math.log(max(v, floor)) for v in row] for name, row in tables.items()}


def _one_fit(
    data: PatternCounts,
    rng: random.Random,
    jitter: float,
    smoothing: float,
    u_floor: float,
    max_iter: int,
    tolerance: float,
    warm_start: LabelledSlice | None = None,
    clamped: ClampedPatterns | None = None,
    init: FellegiSunterModel | None = None,
    label_noise: float = 0.0,
) -> tuple[
    dict[str, list[float]], dict[str, list[float]], float, int, float, list[dict[str, float]]
]:
    """One EM run from one start. Returns the fitted tables and its trace."""
    names = FIELD_NAMES[data.kind]
    sizes = LEVEL_COUNTS[data.kind]
    if init is not None:
        m = {k: list(v) for k, v in init.m.items()}
        u = {k: list(v) for k, v in init.u.items()}
        lam = init.lam
    elif warm_start is not None:
        m, u, lam = _warm_start_tables(data.kind, warm_start, smoothing)
    else:
        m, u, lam = _initial_tables(data.kind, rng, jitter)
    fixed = clamped if clamped is not None and clamped.total else None
    # log P(verdict | class): right with 1 - epsilon, wrong with epsilon. A
    # wrong verdict with epsilon = 0 is impossible, hence -inf, which makes the
    # responsibility exactly the label.
    log_right = math.log(1.0 - label_noise)
    log_wrong = math.log(label_noise) if label_noise > 0 else -math.inf

    trace: list[dict[str, float]] = []
    previous = -math.inf
    iteration = 0
    log_likelihood = -math.inf

    for iteration in range(1, max_iter + 1):
        log_m = _log_tables(m, DEFAULT_M_FLOOR)
        log_u = _log_tables(u, u_floor)
        log_lam = math.log(max(lam, 1e-12))
        log_one_minus = math.log(max(1.0 - lam, 1e-12))

        # E-step: the posterior responsibility of the match component for each
        # distinct pattern, weighted by how often the pattern occurred.
        responsibilities: list[float] = []
        log_likelihood = 0.0
        for pattern, count in zip(data.patterns, data.counts, strict=True):
            a = log_lam
            b = log_one_minus
            for name, level in zip(names, pattern, strict=True):
                a += log_m[name][level]
                b += log_u[name][level]
            responsibilities.append(sigmoid(a - b))
            log_likelihood += count * _log_add(a, b)
        labelled_g: list[float] = []
        if fixed is not None:
            # A labelled pair contributes the probability of its vector *and*
            # its verdict, and its responsibility is conditioned on both.
            for pattern, label, count in zip(
                fixed.patterns, fixed.labels, fixed.counts, strict=True
            ):
                a = log_lam + (log_right if label else log_wrong)
                b = log_one_minus + (log_wrong if label else log_right)
                for name, level in zip(names, pattern, strict=True):
                    a += log_m[name][level]
                    b += log_u[name][level]
                labelled_g.append(sigmoid(a - b))
                log_likelihood += count * _log_add(a, b)

        # M-step: re-estimate every table from the weighted level counts.
        # `marginal` is the overall level distribution, and it is what both
        # components are smoothed toward - see the module docstring.
        match_mass = 0.0
        m_counts = {name: [0.0] * size for name, size in zip(names, sizes, strict=True)}
        u_counts = {name: [0.0] * size for name, size in zip(names, sizes, strict=True)}
        for pattern, count, g in zip(data.patterns, data.counts, responsibilities, strict=True):
            weighted = count * g
            complement = count - weighted
            match_mass += weighted
            for name, level in zip(names, pattern, strict=True):
                m_counts[name][level] += weighted
                u_counts[name][level] += complement
        if fixed is not None:
            for pattern, count, g in zip(fixed.patterns, fixed.counts, labelled_g, strict=True):
                weighted = count * g
                match_mass += weighted
                for name, level in zip(names, pattern, strict=True):
                    m_counts[name][level] += weighted
                    u_counts[name][level] += count - weighted

        total = float(data.total + (fixed.total if fixed is not None else 0))
        lam = match_mass / total if total else 0.0
        non_match_mass = total - match_mass
        for name, size in zip(names, sizes, strict=True):
            marginal = _marginal(m_counts[name], u_counts[name], size)
            m[name] = _smoothed(m_counts[name], smoothing, size, marginal, match_mass, total)
            u[name] = _floored(
                _smoothed(u_counts[name], smoothing, size, marginal, non_match_mass, total),
                u_floor,
            )

        trace.append(
            {
                "iteration": iteration,
                "log_likelihood": round(log_likelihood, 6),
                "lambda": round(lam, 8),
                "delta": round(log_likelihood - previous, 9) if previous > -math.inf else 0.0,
            }
        )
        if previous > -math.inf and abs(log_likelihood - previous) < tolerance:
            break
        previous = log_likelihood

    return m, u, lam, iteration, log_likelihood, trace


def _marginal(m_counts: Sequence[float], u_counts: Sequence[float], size: int) -> list[float]:
    """The overall level distribution, itself Laplace-smoothed so it has no zeros."""
    combined = [a + b for a, b in zip(m_counts, u_counts, strict=True)]
    denominator = sum(combined) + size
    return [(c + 1.0) / denominator for c in combined]


def _smoothed(
    counts: Sequence[float],
    alpha: float,
    size: int,
    marginal: Sequence[float],
    mass: float,
    total: float,
) -> list[float]:
    """Relative frequencies, backed off toward `marginal` by a mass-proportional prior.

    The pseudo-mass is `alpha * size * (mass / total)`, so the *fraction* of a
    component made up of prior is the same for the match and the non-match
    tables however unbalanced they are. A level with no observations therefore
    ends up with the same value in both, and contributes a weight of zero
    rather than an artefact of the class imbalance.
    """
    share = (mass / total) if total else 0.0
    prior = alpha * size * share
    denominator = mass + prior
    if denominator <= 0:
        return list(marginal)
    return [(c + prior * q) / denominator for c, q in zip(counts, marginal, strict=True)]


def _floored(row: Sequence[float], floor: float) -> list[float]:
    """Apply the u floor, then renormalize so the row is still a distribution."""
    lifted = [max(v, floor) for v in row]
    return _normalize(lifted)


def _mean_agreement(tables: Mapping[str, list[float]], kind: ModelKind) -> float:
    """Expected agreement level under a set of tables, normalized per field.

    Used only to decide which EM component is the match class. The algorithm
    itself is indifferent to the labels, so they are assigned from the fit
    rather than assumed - the alternative is a model that occasionally comes out
    exactly inverted and scores every true pair as a non-match.
    """
    total = 0.0
    names = FIELD_NAMES[kind]
    for name in names:
        row = tables[name]
        top = max(len(row) - 1, 1)
        total += sum(level * p for level, p in enumerate(row)) / top
    return total / len(names)


def fit_em(
    data: PatternCounts,
    seed: int = 0,
    restarts: int = DEFAULT_RESTARTS,
    smoothing: float = DEFAULT_SMOOTHING,
    u_floor: float = DEFAULT_U_FLOOR,
    max_iter: int = DEFAULT_MAX_ITER,
    tolerance: float = DEFAULT_TOLERANCE,
    min_pairs: int = 50,
    warm_start: LabelledSlice | None = None,
    clamped: ClampedPatterns | None = None,
    init: FellegiSunterModel | None = None,
    label_noise: float = 0.0,
) -> FellegiSunterModel:
    """Fit m, u and lambda by EM. Deterministic under `seed`.

    `warm_start`, when given, initializes the *first* restart from a labelled
    slice; the remaining restarts still begin from perturbed generic priors, so
    a bad slice cannot trap the search. It is optional by design - the default
    fit uses no labels at all.

    `clamped` makes the fit semi-supervised: those pairs carry their verdicts
    into every E-step, each wrong with probability `label_noise` (see the
    module docstring). `init` starts the first restart
    from an existing model's tables - a retune begins where its parent config
    ended, not from the generic prior. Neither changes the other restarts.

    Raises `DegenerateFitError` when the fit found one component rather than
    two, or when there are too few pairs for the estimate to mean anything -
    the organization model routinely has an order of magnitude fewer pairs than
    the individual one, and quietly serving a model fitted on forty pairs is
    worse than refusing to.
    """
    if not 0.0 <= label_noise < 0.5:
        raise ValueError(f"label_noise must be in [0, 0.5), got {label_noise}")
    observed = data.total + (clamped.total if clamped is not None else 0)
    if observed < min_pairs:
        raise DegenerateFitError(
            f"{data.kind} fit needs at least {min_pairs} candidate pairs, got {observed}"
        )

    best: tuple[float, Any] | None = None
    for restart in range(restarts):
        # One generator per restart, seeded from the run seed, so restart k is
        # the same draw whether or not restart k-1 ran.
        rng = random.Random((seed << 8) ^ restart)
        jitter = 0.0 if restart == 0 else 0.35
        start = warm_start if restart == 0 else None
        result = _one_fit(
            data,
            rng,
            jitter,
            smoothing,
            u_floor,
            max_iter,
            tolerance,
            start,
            clamped=clamped,
            init=init if restart == 0 else None,
            label_noise=label_noise,
        )
        log_likelihood = result[4]
        if best is None or log_likelihood > best[0]:
            best = (log_likelihood, result)

    assert best is not None
    m, u, lam, iterations, log_likelihood, trace = best[1]

    # Label switching: EM does not know which component is the match class -
    # unless labelled pairs were clamped, in which case the labels already
    # named it and swapping would contradict them.
    semi = clamped is not None and clamped.total > 0
    if not semi and _mean_agreement(u, data.kind) > _mean_agreement(m, data.kind):
        m, u = u, m
        lam = 1.0 - lam
        for row in trace:
            row["lambda"] = round(1.0 - row["lambda"], 8)

    if not LAMBDA_MIN <= lam <= LAMBDA_MAX:
        raise DegenerateFitError(
            f"{data.kind} fit degenerate: lambda collapsed to {lam:.3e} "
            f"over {data.total} pairs - the mixture found one component, not two"
        )

    return FellegiSunterModel(
        kind=data.kind,
        fields=FIELD_NAMES[data.kind],
        m=m,
        u=u,
        lam=lam,
        u_floor=u_floor,
        smoothing=smoothing,
        n_pairs=observed,
        n_patterns=len(data.patterns),
        seed=seed,
        restarts=restarts,
        warm_started=warm_start is not None or init is not None,
        iterations=iterations,
        log_likelihood=log_likelihood,
        convergence=trace,
    )
