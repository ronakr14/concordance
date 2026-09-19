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
   grey-band record produces is the same whichever of them sends it.
3. Each strategy's true positives, false positives and review load are then
   the exact counts for every stratum it leaves to the engine, plus the
   sample's counts scaled to the stratum's size for every stratum it hands to
   the model. Recall's denominator - how many records truly match - is known
   exactly from ground truth, so it is never estimated.
4. A **stratified bootstrap** (resampling within each stratum, never across)
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
STRATEGIES = ("probabilistic", "routed", "everything")


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

    rng = random.Random(f"{seed}:{level}")
    grey_pick = rng.sample(range(len(grey)), min(sample_per_stratum, len(grey)))
    decided_pick = rng.sample(range(len(decided)), min(sample_per_stratum, len(decided)))
    total_calls = len(grey_pick) + len(decided_pick)

    def ask(result: MatchResult) -> tuple[Any, Any, int, int, bool]:
        request = adjudication_request(engine, result, top_k=top_k)
        before = meter.snapshot()
        outcome = adjudicator.adjudicate(request)
        after = meter.snapshot()
        failed = after[2] > before[2] and outcome.abstained
        return request, outcome, after[0] - before[0], after[1] - before[1], failed

    grey_ok: list[_Sampled] = []
    decided_ok: list[_Sampled] = []
    failed = 0
    done = 0
    for bucket, picks, target in ((grey, grey_pick, grey_ok), (decided, decided_pick, decided_ok)):
        for index in sorted(picks):
            work, result = bucket[index]
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
            target.append(
                _Sampled(
                    work=work,
                    engine=result,
                    routed=answered if bucket is grey else result,
                    everything=everything,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                )
            )

    exact_none = _sum(none)
    exact_decided = _sum(decided_engine)
    exact_grey = _sum(grey_engine)

    def estimates(
        g: Sequence[_Sampled], d: Sequence[_Sampled]
    ) -> dict[str, Estimate]:
        return {
            "probabilistic": _estimate(
                exact_none + exact_grey + exact_decided, [], expected_matches
            ),
            "routed": _estimate(
                exact_none + exact_decided,
                [([contribution(s.work, s.routed) for s in g], len(grey))],
                expected_matches,
            ),
            "everything": _estimate(
                exact_none,
                [
                    ([contribution(s.work, s.everything) for s in g], len(grey)),
                    ([contribution(s.work, s.everything) for s in d], len(decided)),
                ],
                expected_matches,
            ),
        }

    point = estimates(grey_ok, decided_ok)

    # Stratified bootstrap. Contributions are precomputed once per sampled
    # record so each resample is arithmetic, not re-scoring.
    boot_rng = random.Random(f"{seed}:{level}:bootstrap")
    draws: dict[str, dict[str, list[float]]] = {
        name: {"precision": [], "recall": [], "f1": []} for name in ("routed", "everything")
    }
    g_routed = [contribution(s.work, s.routed) for s in grey_ok]
    g_every = [contribution(s.work, s.everything) for s in grey_ok]
    d_every = [contribution(s.work, s.everything) for s in decided_ok]
    if grey_ok or decided_ok:
        for _ in range(bootstrap):
            gi = [boot_rng.randrange(len(grey_ok)) for _ in grey_ok]
            di = [boot_rng.randrange(len(decided_ok)) for _ in decided_ok]
            routed = _estimate(
                exact_none + exact_decided,
                [([g_routed[i] for i in gi], len(grey))],
                expected_matches,
            )
            every = _estimate(
                exact_none,
                [([g_every[i] for i in gi], len(grey)), ([d_every[i] for i in di], len(decided))],
                expected_matches,
            )
            for name, est in (("routed", routed), ("everything", every)):
                draws[name]["precision"].append(est.precision)
                draws[name]["recall"].append(est.recall)
                draws[name]["f1"].append(est.f1)

    strategies: dict[str, Any] = {}
    for name in STRATEGIES:
        row = _metrics(point[name])
        row["exact"] = name == "probabilistic"
        if name in draws:
            row["interval"] = {k: _interval(v) for k, v in draws[name].items()}
        strategies[name] = row

    # Cost: calls are exact (stratum sizes), tokens per call come from the sample.
    grey_prompt = _mean([s.prompt_tokens for s in grey_ok])
    grey_completion = _mean([s.completion_tokens for s in grey_ok])
    dec_prompt = _mean([s.prompt_tokens for s in decided_ok])
    dec_completion = _mean([s.completion_tokens for s in decided_ok])

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
        },
        "sample": {
            "grey": len(grey_ok),
            "decided": len(decided_ok),
            "failed": failed,
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
        routed_f1=strategies["routed"]["f1"],
        everything_f1=strategies["everything"]["f1"],
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
