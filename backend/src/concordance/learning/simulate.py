"""Simulated review rounds: label, retune, rescore, repeat.

GATE 9 asks to *see* the feedback loop move precision and recall, not to be
told it would. Real reviewers take weeks to produce a few hundred labels, so
the rounds are simulated on a synthetic dataset where the answer is known:

- **The starting config is fitted on different data** - a dataset at another
  corruption level. That is the realistic starting point: a config fitted on
  last year's file, or on synthetic data, meeting a file that is messier than
  it expected. Calibrated on its own data it would already be as good as the
  loop can make it, and the chart would show a flat line.
- **Each round, a simulated reviewer works the queue**: the matches and grey
  band the current config produces, plus the random audit of its auto-rejects.
  It picks `per_round` of them at random and labels each with the ground truth
  - naming the right candidate where one is offered, rejecting the top one
  where none is - except that with probability `noise` it gets the answer
  wrong. Real reviewers make mistakes, and a feedback loop that only works with
  perfect labels does not work.
- **Then the config is retuned** on every label so far, and it replaces the
  current one only if the retune's own holdout comparison recommends it - the
  decision an admin makes on the Models page. `policy="always"` activates every
  retune instead, which is how the docs show what the gate is worth.
- **Every round is judged on records no reviewer ever sees** - a fixed
  evaluation split, held out by record hash - against ground truth. Judging on
  the reviewed records would measure how well the config memorized its labels.

Round 0 is the starting config, before any label.
"""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from concordance.domain import Outcome
from concordance.eval.fitting import collect_patterns
from concordance.eval.harness import evaluate
from concordance.eval.pairs import PreparedDataset, RecordWork
from concordance.learning.retune import (
    Label,
    NotEnoughLabelsError,
    label_counts,
    retune,
)
from concordance.logging_setup import get_logger
from concordance.matching.calibration import in_holdout
from concordance.matching.scorer import MatchResult
from concordance.matching.scoring_config import ScoringConfig
from concordance.matching.strategies import ProbabilisticStrategy

log = get_logger("learning.simulate")

DEFAULT_ROUNDS = 5
DEFAULT_PER_ROUND = 200
DEFAULT_NOISE = 0.03
#: Share of records held out of review, to judge every round on.
EVAL_FRACTION = 0.3


def _draw(seed: int, round_no: int, record_id: str) -> float:
    digest = hashlib.sha256(f"sim-audit:{seed}:{round_no}:{record_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _oracle(
    work: RecordWork, result: MatchResult, rng: random.Random, noise: float
) -> tuple[Any, int] | None:
    """The candidate a reviewer labels, and the label - wrong with probability `noise`.

    Where a right provider is among the candidates, a reviewer names it: a
    true match on that pair. Where none is, they reject the one on top. "Right"
    is the same rule the original fit calibrated on (`fitting._label_of`): the
    expected provider, or any of the plausible ones for a record whose truth is
    `AMBIGUOUS` - each of those genuinely is the same kind of person, and
    labelling them non-matches would teach the model that agreement is
    evidence against a match.
    """
    if not result.candidates:
        return None
    expected = work.expected_provider_ids
    matchable = work.truth is not None and work.truth.expected_outcome is not Outcome.NO_MATCH
    right = next(
        (c for c in result.candidates if matchable and c.provider_id in expected), None
    )
    candidate, label = (right, 1) if right is not None else (result.candidates[0], 0)
    if rng.random() < noise:
        # A mistake: the pair the reviewer was looking at, with the wrong verdict.
        label = 1 - label
    return candidate, label


def _judge_on_truth(prepared: PreparedDataset, config: ScoringConfig) -> dict[str, Any]:
    report = evaluate(prepared, ProbabilisticStrategy(config.engine()), show_progress=False)
    overall = report.overall
    return {
        "precision": overall.precision,
        "recall": overall.recall,
        "f1": overall.f1,
        "false_positives": overall.false_positives,
        "false_negatives": overall.false_negatives,
        "review_share": round(report.grey_band_fraction, 6),
        "n": overall.n,
    }


def simulate_rounds(
    prepared: PreparedDataset,
    start: ScoringConfig,
    *,
    seed: int,
    target_precision: float,
    rounds: int = DEFAULT_ROUNDS,
    per_round: int = DEFAULT_PER_ROUND,
    noise: float = DEFAULT_NOISE,
    audit_rate: float = 0.02,
    min_labels: int = 100,
    assumed_noise: float | None = None,
    policy: str = "recommended",
    on_round: Callable[[int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the rounds; return one row per round, round 0 first.

    `noise` is how often the simulated reviewer is wrong; `assumed_noise` is
    the rate the retune is told (`REVIEWER_ERROR_RATE`), the true one unless
    given. Setting them apart is how the docs measure what a mis-measured
    error rate costs.
    """
    assumed = noise if assumed_noise is None else assumed_noise
    started = time.perf_counter()
    evaluation = [w for w in prepared if in_holdout(f"eval:{w.record_id}", seed, EVAL_FRACTION)]
    pool = [w for w in prepared if not in_holdout(f"eval:{w.record_id}", seed, EVAL_FRACTION)]
    held_out = replace(prepared, work=evaluation)
    # The tally is the whole run's, as it would be in production: every record
    # is reconciled, reviewed or not. Labels come from the same run, so they
    # are taken back out of it before a fit (`in_tally`).
    patterns = collect_patterns(prepared.work)
    # Comparison vectors do not depend on the config, so every record's
    # candidates are compared once; each retune rescores them to place its
    # thresholds, as it would on a production run's candidates.
    start_engine = start.engine()
    population = [
        start_engine.score_record(w.normalized, w.candidates).pairs for w in prepared
    ]

    rng = random.Random(f"{seed}:reviewer")
    labels: list[Label] = []
    reviewed: set[str] = set()
    config = start
    history: list[dict[str, Any]] = []

    def record(round_no: int, retuned: dict[str, Any] | None) -> None:
        row: dict[str, Any] = {
            "round": round_no,
            "config_id": config.config_id,
            "labels": label_counts(labels),
            "truth": _judge_on_truth(held_out, config),
            "retune": retuned,
        }
        history.append(row)
        log.info(
            "simulate.round",
            round=round_no,
            labels=len(labels),
            precision=row["truth"]["precision"],
            recall=row["truth"]["recall"],
            f1=row["truth"]["f1"],
        )
        if on_round is not None:
            on_round(round_no, row)

    record(0, None)
    for round_no in range(1, rounds + 1):
        engine = config.engine()
        queue: list[tuple[RecordWork, MatchResult, bool]] = []
        for work in pool:
            if work.record_id in reviewed:
                continue
            result = engine.score_record(work.normalized, work.candidates)
            if result.decision in (Outcome.MATCH, Outcome.AMBIGUOUS):
                queue.append((work, result, False))
            elif result.candidates and _draw(seed, round_no, work.record_id) < audit_rate:
                queue.append((work, result, True))

        picked = rng.sample(queue, min(per_round, len(queue)))
        for work, result, audited in picked:
            reviewed.add(work.record_id)
            verdict = _oracle(work, result, rng, noise)
            if verdict is None:
                continue
            candidate, label = verdict
            labels.append(
                Label(
                    key=work.record_id,
                    kind=candidate.kind,
                    vector=candidate.vector,
                    label=label,
                    # Everything in the queue had the same chance of being
                    # picked; an audited auto-reject also had to be drawn first.
                    weight=(1.0 / audit_rate) if audited else 1.0,
                    source="audit" if audited else "review",
                    in_tally=True,
                )
            )

        retuned: dict[str, Any] | None = None
        try:
            fitted = retune(
                config,
                patterns,
                labels,
                config_id=f"{start.config_id}.sim{round_no}",
                seed=seed,
                target_precision=target_precision,
                min_labels=min_labels,
                label_noise=assumed,
                population=population,
            )
            activated = policy == "always" or fitted.recommended
            if activated:
                config = fitted.config
            retuned = {
                "improved": fitted.improved,
                "activated": activated,
                "verdict": fitted.verdict,
                **fitted.metrics,
            }
        except NotEnoughLabelsError as exc:
            retuned = {"skipped": str(exc)}
        record(round_no, retuned)

    return {
        "rounds": history,
        "params": {
            "rounds": rounds,
            "per_round": per_round,
            "noise": noise,
            "assumed_noise": assumed,
            "policy": policy,
            "audit_rate": audit_rate,
            "min_labels": min_labels,
            "eval_fraction": EVAL_FRACTION,
            "target_precision": target_precision,
            "seed": seed,
        },
        "records": {"evaluation": len(evaluation), "pool": len(pool)},
        "seconds": round(time.perf_counter() - started, 1),
    }


__all__ = ["simulate_rounds"]

