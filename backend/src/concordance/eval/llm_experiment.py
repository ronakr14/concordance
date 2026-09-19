"""What routing only the grey band to an LLM saves, and what it costs in accuracy.

The Lab's cost panel compares two ways of using a model:

- **routed** - the engine decides what it is sure of and hands only the grey
  band to the adjudicator. This is what the system does.
- **everything** - every record that has a candidate goes to the model, and the
  model's answer is final. This is what a system built LLM-first would do.

Measuring "everything" in full means a model call for nearly every record at
every corruption level, which the free tiers the system is developed on cannot
sustain. So both are measured on a **stratified sample** and extrapolated, and
the extrapolation is done the way a survey statistician would do it rather than
by multiplying one number by another:

1. Every record is scored by the engine first. That splits the population into
   three strata whose sizes are known exactly: records with no candidate
   (no strategy can call a model on these, so every strategy agrees), records
   the engine sent to the grey band, and records the engine decided.
2. A sample is drawn from each of the last two, and only the sample is sent to
   the model. The grey-band sample serves both strategies - the request a
   grey-band record produces is the same whichever of them sends it. Within
   each stratum the sample is split, in proportion, across two cells: records
   that truly match and records that do not. Their sizes are known exactly
   from ground truth, which the Lab already scores against.
3. Each strategy's true positives, false positives and review load are then
   the exact counts for every stratum it leaves to the engine, plus each
   cell's sample counts scaled to that cell's size for every stratum it hands
   to the model. Recall's denominator - how many records truly match - is known
   exactly from ground truth, so it is never estimated, and because a true
   positive can only come from a match cell, estimated recall cannot pass 1.
4. A **stratified bootstrap** (resampling within each cell, never across)
   gives the interval. A 200-record sample cannot tell 0.95 from 0.96, and the
   chart should say so rather than draw two points as though it could.

Tokens are metered per call, cache hits included - a cached answer is free to
replay but cost the same to produce - and priced at a reference production
model, because the development models are free and "$0 versus $0" is not a
finding. The price is a placeholder and the payload says so.

Two kinds of non-answer are kept apart. A model that looks at the evidence and
declines is an answer - `AMBIGUOUS`, which for the routed strategy is what the
engine had already said, and for the everything baseline means a human
reviews it. A call that never completed (every provider rate-limited or down)
is not an answer about the record at all, so that record is dropped from the
sample and the drop is reported, rather than being scored as though the model
had chosen to abstain.

Dropping is only safe if the calls that survive are still a random sample, and
the obvious loop breaks that. Synthetic files are written scenario by scenario
- every exact-NPI match first, the unmatched records last - so walking the
sample in file order and running out of daily quota half way keeps the matches
and drops the non-matches, and the scaled-up estimate reports more true
positives than there are matches (a first real run did exactly that: F1 1.05).
So the sample is called in a seeded random order, interleaved across both
strata, and a quota cut leaves a smaller random sample rather than a biased
one. A stratum left with fewer than `min_answered` answers is not estimated at
all: the strategies that depend on it are withheld at that level rather than
extrapolated from a handful of records.
"""

from __future__ import annotations

import random
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from concordance.domain import Outcome
from concordance.eval.harness import Tally, _classify
from concordance.eval.pairs import PreparedDataset, RecordWork
from concordance.llm.errors import LLMError
from concordance.llm.pricing import ModelPrice
from concordance.llm.types import LLMResponse
from concordance.logging_setup import get_logger
from concordance.matching.scorer import MatchingEngine, MatchResult
from concordance.matching.strategies import adjudication_request, apply_adjudication

log = get_logger("eval.llm_experiment")

DEFAULT_SAMPLE = 100
DEFAULT_BOOTSTRAP = 1_000
#: Fewer answered calls than this in a stratum and it is not extrapolated.
DEFAULT_MIN_ANSWERED = 30
STRATEGIES = ("probabilistic", "routed", "everything")
#: The sampled strata each estimated strategy is extrapolated from.
DEPENDS_ON = {"routed": ("grey",), "everything": ("grey", "decided")}
#: Sampling cells: stratum x whether the record truly matches.
CELLS = ("grey:match", "grey:other", "decided:match", "decided:other")
#: Each populated cell gets at least this many picks, where the stratum's sample allows.
MIN_CELL = 5


# --------------------------------------------------------------------------
# metering
# --------------------------------------------------------------------------


@dataclass
class TokenMeter:
    """Counts what every call used, whether or not it reached the network."""

    calls: int = 0
    live: int = 0
    cached: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def snapshot(self) -> tuple[int, int, int]:
        return self.prompt_tokens, self.completion_tokens, self.failures


@dataclass
class MeteredRouter:
    """A router that also records each response's tokens.

    The router's own stats count tokens only for live calls, which is right for
    a bill and wrong for this experiment: a rerun served from cache would report
    that the baseline costs nothing. Wrapping rather than changing the router
    keeps that accounting where it belongs.
    """

    inner: Any
    meter: TokenMeter = field(default_factory=TokenMeter)

    @property
    def available(self) -> bool:
        return bool(self.inner.available)

    def complete(self, messages: Any, schema: Any = None, **opts: Any) -> LLMResponse:
        self.meter.calls += 1
        try:
            response: LLMResponse = self.inner.complete(messages, schema, **opts)
        except LLMError:
            self.meter.failures += 1
            raise
        self.meter.prompt_tokens += response.prompt_tokens
        self.meter.completion_tokens += response.completion_tokens
        if response.cached:
            self.meter.cached += 1
        else:
            self.meter.live += 1
        return response

    def stats_dict(self) -> dict[str, Any]:
        return dict(self.inner.stats_dict())

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()


# --------------------------------------------------------------------------
# per-record contributions
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Contribution:
    """One record's share of the totals a strategy is judged on."""

    tp: int = 0
    fp: int = 0
    review: int = 0

    def __add__(self, other: Contribution) -> Contribution:
        return Contribution(self.tp + other.tp, self.fp + other.fp, self.review + other.review)


def contribution(work: RecordWork, result: MatchResult) -> Contribution:
    tally = Tally()
    _classify(work, result, tally)
    return Contribution(
        tp=tally.true_positives,
        fp=tally.false_positives,
        review=int(result.decision is Outcome.AMBIGUOUS),
    )


def _sum(items: Sequence[Contribution]) -> Contribution:
    total = Contribution()
    for item in items:
        total = total + item
    return total


@dataclass(frozen=True, slots=True)
class Estimate:
    tp: float
    fp: float
    review: float
    expected_matches: int

    @property
    def precision(self) -> float:
        denominator = self.tp + self.fp
        return self.tp / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        return self.tp / self.expected_matches if self.expected_matches else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _scaled(sample: Sequence[Contribution], population: int) -> tuple[float, float, float]:
    """A stratum's totals, estimated from its sample."""
    if not sample:
        return 0.0, 0.0, 0.0
    factor = population / len(sample)
    total = _sum(sample)
    return total.tp * factor, total.fp * factor, total.review * factor


def _estimate(
    exact: Contribution,
    sampled: Sequence[tuple[Sequence[Contribution], int]],
    expected_matches: int,
) -> Estimate:
    tp, fp, review = float(exact.tp), float(exact.fp), float(exact.review)
    for sample, population in sampled:
        s_tp, s_fp, s_review = _scaled(sample, population)
        tp, fp, review = tp + s_tp, fp + s_fp, review + s_review
    return Estimate(tp, fp, review, expected_matches)


def _interval(values: list[float]) -> list[float]:
    if not values:
        return [0.0, 0.0]
    ordered = sorted(values)
    lo = ordered[int(0.025 * (len(ordered) - 1))]
    hi = ordered[int(0.975 * (len(ordered) - 1))]
    return [round(lo, 6), round(hi, 6)]


def _metrics(estimate: Estimate) -> dict[str, Any]:
    return {
        "precision": round(estimate.precision, 6),
        "recall": round(estimate.recall, 6),
        "f1": round(estimate.f1, 6),
        "review": round(estimate.review, 1),
        "true_positives": round(estimate.tp, 1),
        "false_positives": round(estimate.fp, 1),
    }


# --------------------------------------------------------------------------
# the experiment
# --------------------------------------------------------------------------


@dataclass
class _Sampled:
    work: RecordWork
    engine: MatchResult
    routed: MatchResult
    everything: MatchResult
    prompt_tokens: int
    completion_tokens: int


def _mean(values: list[int]) -> float:
    return statistics.fmean(values) if values else 0.0


def _allocate(sizes: Sequence[int], n: int) -> list[int]:
    """Split a stratum's sample of `n` across its cells, proportional to size.

    Largest-remainder rounding, so the parts sum to exactly
    `min(n, sum(sizes))`. A populated cell that rounds below `MIN_CELL` is then
    topped up from the largest part, so a small cell is never left without a
    record to scale from.
    """
    total = sum(sizes)
    n = min(n, total)
    if not n:
        return [0] * len(sizes)
    shares = [n * size / total for size in sizes]
    parts = [int(share) for share in shares]
    by_remainder = sorted(range(len(sizes)), key=lambda i: -(shares[i] - parts[i]))
    for i in by_remainder[: n - sum(parts)]:
        parts[i] += 1
    populated = sum(1 for s in sizes if s)
    for i, size in enumerate(sizes):
        floor = min(size, MIN_CELL, n // populated)
        while parts[i] < floor:
            donor = max(range(len(parts)), key=lambda j: parts[j])
            if parts[donor] <= floor:
                break
            parts[donor] -= 1
            parts[i] += 1
    return parts


def _tokens_per_call(
    cells: dict[str, list[Any]], ok: dict[str, list[_Sampled]], stratum: str
) -> tuple[float, float]:
    """Mean prompt and completion tokens per call in a stratum, cells weighted by size."""
    names = [c for c in CELLS if c.startswith(f"{stratum}:") and ok[c]]
    weight = sum(len(cells[c]) for c in names)
    if not weight:
        return 0.0, 0.0
    prompt = sum(len(cells[c]) * _mean([s.prompt_tokens for s in ok[c]]) for c in names)
    completion = sum(len(cells[c]) * _mean([s.completion_tokens for s in ok[c]]) for c in names)
    return prompt / weight, completion / weight


def run_llm_experiment(
    prepared: PreparedDataset,
    engine: MatchingEngine,
    adjudicator: Any,
    meter: TokenMeter,
    price_model: str,
    price: ModelPrice,
    seed: int,
    sample_per_stratum: int = DEFAULT_SAMPLE,
    top_k: int = 3,
    bootstrap: int = DEFAULT_BOOTSTRAP,
    on_progress: Callable[[int, int], None] | None = None,
    min_answered: int = DEFAULT_MIN_ANSWERED,
) -> dict[str, Any]:
    """Score everything, sample two strata, adjudicate the sample, extrapolate.

    `adjudicator` must be built on a `MeteredRouter` whose meter is `meter`;
    the experiment reads the meter around each call to attribute tokens and
    failures to the record that caused them.
    """
    started = time.perf_counter()
    level = prepared.corruption_level

    none: list[Contribution] = []
    grey: list[tuple[RecordWork, MatchResult]] = []
    decided: list[tuple[RecordWork, MatchResult]] = []
    decided_engine: list[Contribution] = []
    grey_engine: list[Contribution] = []
    expected_matches = 0
    for work in prepared:
        result = engine.score_record(work.normalized, work.candidates)
        if work.truth is not None and work.truth.expected_outcome is Outcome.MATCH:
            expected_matches += 1
        if not result.candidates:
            none.append(contribution(work, result))
        elif result.decision is Outcome.AMBIGUOUS:
            grey.append((work, result))
            grey_engine.append(contribution(work, result))
        else:
            decided.append((work, result))
            decided_engine.append(contribution(work, result))

    # Sample within cells: stratum x whether the record truly matches. The
    # match count of every cell is known exactly from ground truth, so scaling
    # each cell by its own size means true positives can only come from match
    # cells and never exceed them - recall cannot pass 1 on sampling noise, and
    # the variance falls because its biggest source is stratified away.
    cells: dict[str, list[tuple[RecordWork, MatchResult]]] = {name: [] for name in CELLS}
    for stratum, members in (("grey", grey), ("decided", decided)):
        for work, result in members:
            truly = work.truth is not None and work.truth.expected_outcome is Outcome.MATCH
            cells[f"{stratum}:{'match' if truly else 'other'}"].append((work, result))

    rng = random.Random(f"{seed}:{level}")
    picks: dict[str, list[int]] = {}
    for stratum in ("grey", "decided"):
        names = [c for c in CELLS if c.startswith(f"{stratum}:")]
        sizes = [len(cells[c]) for c in names]
        for name, n in zip(names, _allocate(sizes, sample_per_stratum), strict=True):
            picks[name] = rng.sample(range(len(cells[name])), n)
    total_calls = sum(len(v) for v in picks.values())

    def ask(result: MatchResult) -> tuple[Any, Any, int, int, bool]:
        request = adjudication_request(engine, result, top_k=top_k)
        before = meter.snapshot()
        outcome = adjudicator.adjudicate(request)
        after = meter.snapshot()
        failed = after[2] > before[2] and outcome.abstained
        return request, outcome, after[0] - before[0], after[1] - before[1], failed

    # The call order is random and interleaved across cells, so a run cut short
    # by a quota leaves a random subsample of each - see the module docstring
    # for what file order does instead.
    order = [(name, i) for name in CELLS for i in picks[name]]
    random.Random(f"{seed}:{level}:order").shuffle(order)

    ok: dict[str, list[_Sampled]] = {name: [] for name in CELLS}
    failed = 0
    done = 0
    for name, index in order:
        work, result = cells[name][index]
        is_grey = name.startswith("grey:")
        request, outcome, prompt, completion, call_failed = ask(result)
        done += 1
        if on_progress is not None:
            on_progress(done, total_calls)
        if call_failed:
            failed += 1
            continue
        answered = apply_adjudication(result, request, outcome)
        # Routed: only the grey band is ever sent, so a decided record keeps
        # the engine's answer. Everything: the model is the decider, so a
        # considered abstention is a record for a human.
        everything = answered if not outcome.abstained else _to_review(result)
        ok[name].append(
            _Sampled(
                work=work,
                engine=result,
                routed=answered if is_grey else result,
                everything=everything,
                prompt_tokens=prompt,
                completion_tokens=completion,
            )
        )
    grey_ok = ok["grey:match"] + ok["grey:other"]
    decided_ok = ok["decided:match"] + ok["decided:other"]

    # A stratum is estimable when at least `min_answered` of its calls
    # answered - or all of them, where fewer were planned - and no populated
    # cell in it was left with nothing to scale. The bar is against the planned
    # sample, not the stratum, so a small sample asked for deliberately is
    # reported with the wide interval it has; the guard is for samples a quota
    # cut short. An empty stratum needs no sample at all.
    def planned(stratum: str) -> int:
        return sum(len(picks[c]) for c in CELLS if c.startswith(f"{stratum}:"))

    answered_by = {"grey": (len(grey_ok), len(grey)), "decided": (len(decided_ok), len(decided))}
    short = sorted(
        stratum
        for stratum, (got, size) in answered_by.items()
        if size
        and (
            got < min(min_answered, planned(stratum))
            or any(cells[c] and not ok[c] for c in CELLS if c.startswith(f"{stratum}:"))
        )
    )

    exact_none = _sum(none)
    exact_decided = _sum(decided_engine)
    exact_grey = _sum(grey_engine)

    # Contributions are computed once per sampled record, so the bootstrap's
    # resamples are arithmetic rather than re-scoring.
    routed_c = {c: [contribution(s.work, s.routed) for s in ok[c]] for c in CELLS}
    every_c = {c: [contribution(s.work, s.everything) for s in ok[c]] for c in CELLS}

    def estimates(
        pick: Callable[[str, list[Contribution]], list[Contribution]],
    ) -> dict[str, Estimate]:
        return {
            "probabilistic": _estimate(
                exact_none + exact_grey + exact_decided, [], expected_matches
            ),
            "routed": _estimate(
                exact_none + exact_decided,
                [(pick(c, routed_c[c]), len(cells[c])) for c in CELLS if c.startswith("grey:")],
                expected_matches,
            ),
            "everything": _estimate(
                exact_none,
                [(pick(c, every_c[c]), len(cells[c])) for c in CELLS],
                expected_matches,
            ),
        }

    point = estimates(lambda _c, items: items)

    # Stratified bootstrap: resample within each cell, never across. The same
    # indices serve both strategies, because they were measured on the same
    # calls.
    boot_rng = random.Random(f"{seed}:{level}:bootstrap")
    draws: dict[str, dict[str, list[float]]] = {
        name: {"precision": [], "recall": [], "f1": []} for name in ("routed", "everything")
    }
    if grey_ok or decided_ok:
        for _ in range(bootstrap):
            drawn = {c: [boot_rng.randrange(len(ok[c])) for _ in ok[c]] for c in CELLS}

            def resample(
                c: str, items: list[Contribution], drawn: dict[str, list[int]] = drawn
            ) -> list[Contribution]:
                return [items[i] for i in drawn[c]]

            resampled = estimates(resample)
            for name in ("routed", "everything"):
                est = resampled[name]
                draws[name]["precision"].append(est.precision)
                draws[name]["recall"].append(est.recall)
                draws[name]["f1"].append(est.f1)

    strategies: dict[str, Any] = {}
    withheld: list[str] = []
    for name in STRATEGIES:
        if any(stratum in short for stratum in DEPENDS_ON.get(name, ())):
            withheld.append(name)
            continue
        row = _metrics(point[name])
        row["exact"] = name == "probabilistic"
        if name in draws:
            row["interval"] = {k: _interval(v) for k, v in draws[name].items()}
        strategies[name] = row

    # Cost: calls are exact (stratum sizes), tokens per call come from the
    # sample, each cell weighted by its share of the stratum.
    grey_prompt, grey_completion = _tokens_per_call(cells, ok, "grey")
    dec_prompt, dec_completion = _tokens_per_call(cells, ok, "decided")

    def spend(calls_grey: int, calls_decided: int) -> dict[str, Any]:
        prompt = calls_grey * grey_prompt + calls_decided * dec_prompt
        completion = calls_grey * grey_completion + calls_decided * dec_completion
        return {
            "calls": calls_grey + calls_decided,
            "prompt_tokens": round(prompt),
            "completion_tokens": round(completion),
            "tokens": round(prompt + completion),
            "usd": round(price.cost(round(prompt), round(completion)), 6),
        }

    routed_spend = spend(len(grey), 0)
    every_spend = spend(len(grey), len(decided))

    def saving(key: str) -> float | None:
        base = every_spend[key]
        return round(1 - routed_spend[key] / base, 6) if base else None

    notes: list[str] = []
    if failed:
        notes.append(
            f"{failed} sampled call(s) never completed (provider errors) and were "
            "dropped from the sample rather than scored as abstentions"
        )
    if withheld:
        detail = ", ".join(
            f"{name} {answered_by[name][0]} of {planned(name)}"
            for name in short
        )
        notes.append(
            f"too few calls answered to extrapolate ({detail}; at least "
            f"{min_answered} needed): {', '.join(withheld)} not estimated at this level"
        )
    if not grey:
        notes.append("the engine sent nothing to the grey band at this level")
    if price.free or (price.prompt_per_million == 0 and price.completion_per_million == 0):
        notes.append(f"reference model {price_model} is priced at zero; dollars will read 0")

    payload = {
        "corruption_level": level,
        "seed": seed,
        "sample_per_stratum": sample_per_stratum,
        "bootstrap": bootstrap,
        "top_k": top_k,
        "population": {
            "records": len(prepared),
            "no_candidates": len(none),
            "grey": len(grey),
            "decided": len(decided),
            "expected_matches": expected_matches,
            "cells": {c: len(cells[c]) for c in CELLS},
        },
        "sample": {
            "grey": len(grey_ok),
            "decided": len(decided_ok),
            "failed": failed,
            "cells": {c: len(ok[c]) for c in CELLS},
            "min_answered": min_answered,
            "withheld": withheld,
            "live_calls": meter.live,
            "cache_hits": meter.cached,
            "seconds": round(time.perf_counter() - started, 1),
        },
        "strategies": strategies,
        "cost": {
            "price_model": price_model,
            "price": {
                "prompt_per_million": price.prompt_per_million,
                "completion_per_million": price.completion_per_million,
            },
            "placeholder_price": True,
            "tokens_per_call": {
                "grey": {"prompt": round(grey_prompt, 1), "completion": round(grey_completion, 1)},
                "decided": {"prompt": round(dec_prompt, 1), "completion": round(dec_completion, 1)},
            },
            "routed": routed_spend,
            "everything": every_spend,
            "saving": {k: saving(k) for k in ("calls", "tokens", "usd")},
        },
        "notes": notes,
    }
    log.info(
        "llm_experiment.done",
        corruption=level,
        grey=len(grey),
        decided=len(decided),
        sampled=len(grey_ok) + len(decided_ok),
        failed=failed,
        withheld=withheld,
        routed_f1=strategies.get("routed", {}).get("f1"),
        everything_f1=strategies.get("everything", {}).get("f1"),
    )
    return payload


def _to_review(result: MatchResult) -> MatchResult:
    """The everything baseline's reading of a model that declined: a human decides."""
    return replace(result, decision=Outcome.AMBIGUOUS, chosen_provider_id=None)


__all__ = [
    "DEFAULT_BOOTSTRAP",
    "DEFAULT_SAMPLE",
    "Contribution",
    "Estimate",
    "MeteredRouter",
    "TokenMeter",
    "contribution",
    "run_llm_experiment",
]
