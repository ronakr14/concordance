"""Evaluating a strategy against ground truth.

The metrics are chosen so that a system cannot look good by cheating in either
direction, which is the only reason a number here is worth reading:

- **Precision counts a wrong provider as a false positive**, not as a match.
  Deciding `MATCH` and naming the wrong person is the expensive error in this
  domain - a provider wrongly flagged as excluded - and a metric that scores it
  as a hit would reward exactly the behaviour the system exists to avoid.
- **`AMBIGUOUS` is a first-class outcome with its own correctness.** Four
  hundred records in the dataset have `AMBIGUOUS` as their *right* answer, and
  a system that auto-decides them is wrong even when it happens to pick the
  provider a human would have picked. `ambiguous_accuracy` reports how often
  refusing to decide was the correct call.
- **Per-scenario breakdown, always.** An aggregate F1 hides which capability
  broke. "Recall fell two points" and "the engine stopped finding people whose
  NPI is missing" are the same number and completely different findings.
- **Individual and organization metrics separately as well as combined**
  (PLAN 11.2). Two models were fitted; reporting one number for both would make
  it impossible to tell which one regressed.
- **Blocking recall is carried through.** It is the ceiling: a pair blocking
  never proposed cannot be scored, and a recall figure reported without it
  invites the wrong fix.

The JSON payload is the shape of an `eval_runs` row (PLAN 6) at the top level,
with everything else under `detail` - so Stage 5 inserts it without reshaping.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from concordance.domain import Outcome
from concordance.eval.pairs import PreparedDataset, RecordWork
from concordance.logging_setup import Progress, get_logger
from concordance.matching.calibration import CalibrationMetrics, reliability
from concordance.matching.comparators import ModelKind
from concordance.matching.scorer import MatchResult, Route
from concordance.matching.strategies import Strategy, StrategyName

log = get_logger("eval.harness")

OUTCOMES = (Outcome.MATCH, Outcome.AMBIGUOUS, Outcome.NO_MATCH)


def _round(value: float | None, places: int = 6) -> float | None:
    return None if value is None else round(value, places)


def _fmt(value: float | None) -> str:
    """A metric that does not apply prints as a dash, never as 0.0000."""
    return "       -" if value is None else f"{value:>8.4f}"


@dataclass
class Tally:
    """Counts for one slice of the evaluation - overall, a scenario, a model."""

    n: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0
    wrong_provider: int = 0
    expected_matches: int = 0
    ambiguous_expected: int = 0
    ambiguous_correct: int = 0

    @property
    def precision(self) -> float | None:
        """`None` where the slice made no positive predictions.

        A scenario whose every record is a true `NO_MATCH` cannot have a
        precision, and printing 0.0000 for it reads as a catastrophic failure
        when the engine in fact got them all right. `accuracy` is the number
        that means something for those slices.
        """
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else None

    @property
    def recall(self) -> float | None:
        """`None` where the slice contains nothing that could have been found."""
        if not self.expected_matches:
            return None
        return self.true_positives / self.expected_matches

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None:
            return None
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        """Right answers over records - defined for every slice, always.

        "Right" means the decision matched the expected outcome, and for a
        `MATCH` it also means naming the correct provider.
        """
        correct = self.true_positives + self.true_negatives + self.ambiguous_correct
        return correct / self.n if self.n else 0.0

    @property
    def ambiguous_accuracy(self) -> float:
        return self.ambiguous_correct / self.ambiguous_expected if self.ambiguous_expected else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "precision": _round(self.precision),
            "recall": _round(self.recall),
            "f1": _round(self.f1),
            "accuracy": round(self.accuracy, 6),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "true_negatives": self.true_negatives,
            "wrong_provider": self.wrong_provider,
            "expected_matches": self.expected_matches,
            "ambiguous_expected": self.ambiguous_expected,
            "ambiguous_correct": self.ambiguous_correct,
            "ambiguous_accuracy": round(self.ambiguous_accuracy, 6),
        }


@dataclass
class EvalReport:
    """Everything one strategy did on one dataset."""

    strategy: StrategyName
    corruption_level: float | None
    dataset: dict[str, Any]
    overall: Tally = field(default_factory=Tally)
    by_model: dict[str, Tally] = field(default_factory=dict)
    by_scenario: dict[str, Tally] = field(default_factory=dict)
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    routes: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    calibration: CalibrationMetrics | None = None
    calibration_by_model: dict[str, CalibrationMetrics] = field(default_factory=dict)
    blocking_recall: float = 0.0
    blocking_evaluated: int = 0
    grey_band_fraction: float = 0.0
    adjudicator: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)
    latency_ms: dict[str, float] = field(default_factory=dict)
    config_id: str = ""
    seed: int = 0
    notes: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    # -- the eval_runs row ------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        calibration = self.calibration
        return {
            "corruption_level": self.corruption_level,
            "strategy": str(self.strategy),
            # The `eval_runs` columns are numeric in the schema, and the
            # overall slice always has both denominators, so the fallback is
            # unreachable in practice and present only to keep the row valid.
            "precision": round(self.overall.precision or 0.0, 6),
            "recall": round(self.overall.recall or 0.0, 6),
            "f1": round(self.overall.f1 or 0.0, 6),
            "false_positives": self.overall.false_positives,
            "false_negatives": self.overall.false_negatives,
            "brier": round(calibration.brier, 6) if calibration else None,
            "ece": round(calibration.ece, 6) if calibration else None,
            "reliability_bins": [b.as_dict() for b in calibration.bins] if calibration else [],
            "detail": {
                "config_id": self.config_id,
                "seed": self.seed,
                "dataset": self.dataset,
                "overall": self.overall.as_dict(),
                "by_model": {k: v.as_dict() for k, v in sorted(self.by_model.items())},
                "by_scenario": {k: v.as_dict() for k, v in sorted(self.by_scenario.items())},
                "confusion": self.confusion,
                "routes": dict(self.routes.most_common()),
                "reasons": dict(self.reasons.most_common()),
                "calibration_by_model": {
                    k: v.as_dict() for k, v in sorted(self.calibration_by_model.items())
                },
                "blocking_recall": round(self.blocking_recall, 6),
                "blocking_evaluated": self.blocking_evaluated,
                "grey_band_fraction": round(self.grey_band_fraction, 6),
                "adjudicator": self.adjudicator,
                "llm": self.llm,
                "latency_ms": {k: round(v, 4) for k, v in self.latency_ms.items()},
                "notes": list(self.notes),
                "failures": self.failures,
            },
        }

    def lines(self, sample_failures: int = 8) -> list[str]:
        out = [
            f"strategy             {self.strategy}",
            f"corruption           {self.corruption_level}",
            f"precision / recall  {_fmt(self.overall.precision)} /{_fmt(self.overall.recall)}"
            f"   F1{_fmt(self.overall.f1)}",
            f"false pos / neg      {self.overall.false_positives} / {self.overall.false_negatives}"
            f"   (wrong provider {self.overall.wrong_provider})",
            f"ambiguous handled    {self.overall.ambiguous_correct}/{self.overall.ambiguous_expected}"
            f"  ({self.overall.ambiguous_accuracy:.4f})",
            f"blocking recall      {self.blocking_recall:.4f}  (ceiling on recall)",
            f"grey band            {self.grey_band_fraction:.1%} of volume",
        ]
        out.append(f"accuracy             {self.overall.accuracy:.4f}")
        if self.calibration:
            out.append(
                f"calibration          ECE {self.calibration.ece:.4f}   "
                f"Brier {self.calibration.brier:.4f}   MCE {self.calibration.mce:.4f}"
            )
        out.append(
            "latency/record       "
            + "  ".join(f"{k} {v:.2f}ms" for k, v in sorted(self.latency_ms.items()))
        )
        if self.llm:
            out.append(
                f"llm                  adjudicator={self.llm['adjudicator']} "
                f"calls={self.llm['calls']} tokens={self.llm['tokens']} "
                f"cost=${self.llm['cost_usd']:.4f}"
            )
        out.append("")
        out.append(f"{'model':<14}{'n':>6}{'prec':>9}{'recall':>9}{'F1':>9}")
        for name, tally in sorted(self.by_model.items()):
            out.append(
                f"{name:<14}{tally.n:>6} {_fmt(tally.precision)}"
                f" {_fmt(tally.recall)} {_fmt(tally.f1)}"
            )
        out.append("")
        out.append(
            f"{'scenario':<24}{'n':>6}{'prec':>9}{'recall':>9}{'F1':>9}{'acc':>9}  correct"
        )
        for name, tally in sorted(self.by_scenario.items()):
            correct = tally.true_positives + tally.true_negatives + tally.ambiguous_correct
            out.append(
                f"{name:<24}{tally.n:>6} {_fmt(tally.precision)}"
                f" {_fmt(tally.recall)} {_fmt(tally.f1)} {tally.accuracy:>8.4f}"
                f"  {correct}/{tally.n}"
            )
        out.append(
            "  (a dash means the metric does not apply: a scenario with no true"
            " matches has no recall to measure)"
        )
        out.append("")
        out.append("confusion (rows expected, columns decided):")
        header = "".join(f"{o:>12}" for o in OUTCOMES)
        out.append(f"{'':<14}{header}")
        for expected in OUTCOMES:
            row = self.confusion.get(str(expected), {})
            out.append(f"{expected:<14}" + "".join(f"{row.get(str(d), 0):>12}" for d in OUTCOMES))
        if self.failures:
            out.append("")
            out.append(f"first {min(sample_failures, len(self.failures))} failures:")
            for failure in self.failures[:sample_failures]:
                out.append(
                    f"  {failure['record_id']:<14} [{failure['scenario']}] "
                    f"expected {failure['expected']}/{failure['expected_provider_id']} "
                    f"got {failure['decision']}/{failure['chosen_provider_id']} "
                    f"conf={failure['confidence']:.4f} reason={failure['reason']}"
                )
        for note in self.notes:
            out.append(f"note: {note}")
        return out


# --------------------------------------------------------------------------
# scoring one record against its truth
# --------------------------------------------------------------------------


def _classify(work: RecordWork, result: MatchResult, tally: Tally) -> str | None:
    """Update `tally` for one record; return a failure description or `None`.

    The rule that matters: a `MATCH` naming the wrong provider is a false
    positive *and* a false negative. It is a false positive because somebody
    will be told they are excluded and they are not, and a false negative
    because the person who actually is excluded was missed. Counting it once
    would flatter whichever metric it was left out of.
    """
    truth = work.truth
    expected = truth.expected_outcome if truth else Outcome.NO_MATCH
    tally.n += 1
    if expected is Outcome.MATCH:
        tally.expected_matches += 1
    if expected is Outcome.AMBIGUOUS:
        tally.ambiguous_expected += 1
        if result.decision is Outcome.AMBIGUOUS:
            tally.ambiguous_correct += 1

    decided = result.decision
    correct_provider = (
        result.chosen_provider_id is not None
        and result.chosen_provider_id in work.expected_provider_ids
    )

    if decided is Outcome.MATCH:
        if expected is Outcome.MATCH and correct_provider:
            tally.true_positives += 1
            return None
        tally.false_positives += 1
        if expected is Outcome.MATCH:
            tally.false_negatives += 1
            if not correct_provider:
                tally.wrong_provider += 1
    elif expected is Outcome.MATCH:
        tally.false_negatives += 1
    elif expected is Outcome.NO_MATCH and decided is Outcome.NO_MATCH:
        tally.true_negatives += 1
        return None
    elif expected is Outcome.AMBIGUOUS and decided is Outcome.AMBIGUOUS:
        return None

    return "failure"


def _failure_row(work: RecordWork, result: MatchResult) -> dict[str, Any]:
    truth = work.truth
    return {
        "record_id": work.record_id,
        "scenario": truth.scenario_tag if truth else "untagged",
        "expected": str(truth.expected_outcome) if truth else "NO_MATCH",
        "expected_provider_id": truth.expected_provider_id if truth else None,
        "decision": str(result.decision),
        "chosen_provider_id": result.chosen_provider_id,
        "confidence": round(result.confidence, 6),
        "reason": str(result.reason),
        "route": str(result.route),
        "levels": dict(result.top.levels) if result.top else {},
    }


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def evaluate(
    prepared: PreparedDataset,
    strategy: Strategy,
    config_id: str = "",
    seed: int = 0,
    keep_failures: int = 200,
    show_progress: bool = True,
) -> EvalReport:
    """Run one strategy over a prepared dataset and score it against truth."""
    report = EvalReport(
        strategy=strategy.name,
        corruption_level=prepared.corruption_level,
        dataset=prepared.summary(),
        config_id=config_id,
        seed=seed,
    )
    confusion: dict[str, dict[str, int]] = {str(e): {str(d): 0 for d in OUTCOMES} for e in OUTCOMES}
    by_model: dict[str, Tally] = defaultdict(Tally)
    by_scenario: dict[str, Tally] = defaultdict(Tally)
    confidences: dict[str, list[float]] = defaultdict(list)
    labels: dict[str, list[int]] = defaultdict(list)
    blocking_found = 0
    blocking_evaluated = 0
    grey = 0
    decide_seconds = 0.0

    with Progress(
        f"eval.{strategy.name}", total=len(prepared), every=1000, enabled=show_progress
    ) as progress:
        for work in prepared:
            progress.tick()
            truth = work.truth
            expected = truth.expected_outcome if truth else Outcome.NO_MATCH

            wanted = work.expected_provider_ids
            if wanted:
                blocking_evaluated += 1
                if any(c.provider_id in wanted for _, c in work.candidates):
                    blocking_found += 1

            started = time.perf_counter()
            result = strategy.decide(work.normalized, work.candidates)
            decide_seconds += time.perf_counter() - started

            confusion[str(expected)][str(result.decision)] += 1
            report.routes[str(result.route)] += 1
            report.reasons[str(result.reason)] += 1
            if result.route is not Route.NO_CANDIDATES and result.decision is Outcome.AMBIGUOUS:
                grey += 1

            model = str(result.kind)
            outcome = _classify(work, result, report.overall)
            _classify(work, result, by_model[model])
            _classify(work, result, by_scenario[truth.scenario_tag if truth else "untagged"])

            top = result.top
            if top is not None:
                # Calibration is measured on the engine's own best candidate and
                # whether that candidate was right - the same quantity the
                # thresholds are applied to, so the reliability diagram
                # describes the decisions actually made.
                confidences[model].append(top.confidence)
                labels[model].append(1 if top.provider_id in wanted else 0)
            if outcome is not None and len(report.failures) < keep_failures:
                report.failures.append(_failure_row(work, result))

    report.confusion = confusion
    report.by_model = dict(by_model)
    report.by_scenario = dict(by_scenario)
    report.blocking_recall = blocking_found / blocking_evaluated if blocking_evaluated else 0.0
    report.blocking_evaluated = blocking_evaluated
    report.grey_band_fraction = grey / len(prepared) if len(prepared) else 0.0
    report.adjudicator = strategy.stats()
    # Reported for every strategy, not only the ones that call a model: a run
    # that made no calls should say zero rather than say nothing, so the sweep
    # rows have the same shape in every cell.
    report.llm = {
        "adjudicator": report.adjudicator.get("adjudicator", "none"),
        "calls": int(report.adjudicator.get("calls", 0)),
        "adjudicated": int(report.adjudicator.get("adjudicated", 0)),
        "tokens": int(report.adjudicator.get("tokens", 0)),
        "cost_usd": float(report.adjudicator.get("cost_usd", 0.0)),
    }

    all_conf = [c for values in confidences.values() for c in values]
    all_labels = [y for values in labels.values() for y in values]
    report.calibration = reliability(all_conf, all_labels)
    for model in confidences:
        report.calibration_by_model[model] = reliability(confidences[model], labels[model])

    n = max(len(prepared), 1)
    stages = prepared.stage_seconds
    report.latency_ms = {
        # Index building is a per-dataset cost, not a per-record one, so it is
        # divided out here only to make the stages comparable on one scale.
        "1_index_build": stages.get("index", 0.0) * 1000 / n,
        "2_normalize": stages.get("normalize", 0.0) * 1000 / n,
        "3_block": stages.get("block", 0.0) * 1000 / n,
        "4_compare_and_score": decide_seconds * 1000 / n,
        "total_per_record": (prepared.prepare_seconds + decide_seconds) * 1000 / n,
    }
    if str(ModelKind.ORGANIZATION) not in report.by_model:
        report.notes.append("no organization records in this dataset slice")

    log.info(
        "eval.done",
        strategy=str(strategy.name),
        precision=_round(report.overall.precision, 4),
        recall=_round(report.overall.recall, 4),
        f1=_round(report.overall.f1, 4),
        ece=round(report.calibration.ece, 4) if report.calibration else None,
    )
    return report


def write_report(report: EvalReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
