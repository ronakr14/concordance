"""The Lab: the LLM experiment's estimator, and the service against the database.

The estimator tests use a scripted "oracle" adjudicator that always names the
ground-truth provider, because the claims being proved are about arithmetic,
not about a model:

1. **With the whole stratum sampled, the estimate is the exact answer.** A
   stratified estimator scaled by a factor of one must agree with simply
   running the strategy over every record - if it does not, the scaling is
   wrong, and no amount of sampling would fix it.
2. **Tokens are metered through the cache.** A rerun served entirely from the
   cache makes no live call and reports the same token cost, because the cost
   panel prices what the answers cost to produce.
3. **A call that never completed is dropped, not scored.**

The service tests run a real (small) sweep and LLM run through the database
and read them back through the API's own response models.
"""

from __future__ import annotations

import json
import random
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from concordance.eval.fitting import fit_config
from concordance.eval.harness import evaluate
from concordance.eval.llm_experiment import MeteredRouter, TokenMeter, run_llm_experiment
from concordance.eval.pairs import prepare
from concordance.llm.ai_matcher import LlmAdjudicator
from concordance.llm.cache import FileCache
from concordance.llm.errors import Transient
from concordance.llm.pricing import ModelPrice
from concordance.llm.router import LLMRouter
from concordance.llm.types import ChatMessage, LLMResponse
from concordance.matching.strategies import ProbabilisticLlmStrategy
from concordance.synth.pipeline import seed_dataset

SEED = 20260914
PROVIDERS = 4_000
SANCTIONS = 900
PRICE = ModelPrice(1.0, 2.0, 131_072)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: Any) -> Any:
    out = tmp_path_factory.mktemp("lab")
    seed_dataset(
        providers=PROVIDERS,
        sanctions=SANCTIONS,
        corruption=0.5,
        seed=SEED,
        out_dir=out,
        write_excel=False,
    )
    return out


@pytest.fixture(scope="module")
def prepared(dataset: Any) -> Any:
    return prepare(dataset, max_candidates=30, show_progress=False)


@pytest.fixture(scope="module")
def engine(prepared: Any) -> Any:
    return fit_config(prepared, seed=SEED).config.engine()


@dataclass
class OracleProvider:
    """Answers every prompt with the ground-truth provider, if it was offered."""

    truth: dict[str, str | None]
    name: str = "oracle"
    model: str = "oracle-1"
    supports_structured_output: bool = False
    calls: int = 0
    fail_every: int = 0
    #: Answer this many calls, then fail every one after - a daily quota running out.
    quota: int = 0
    seen: list[str] = field(default_factory=list)

    def complete(
        self, messages: list[ChatMessage], schema: dict[str, Any] | None = None, **opts: Any
    ) -> LLMResponse:
        self.calls += 1
        if self.fail_every and self.calls % self.fail_every == 0:
            raise Transient("scripted outage", provider=self.name)
        if self.quota and self.calls > self.quota:
            raise Transient("scripted quota exhausted", provider=self.name)
        # The last evidence block is the record being asked about; any earlier
        # ones are the prompt's few-shot examples.
        prompt = next(m.content for m in reversed(messages) if "<<<EVIDENCE" in m.content)
        prompt = prompt.split("<<<EVIDENCE")[-1]
        found = re.search(r"^record: (\S+)", prompt, re.M)
        assert found is not None
        record_id = found.group(1)
        self.seen.append(record_id)
        expected = self.truth[record_id]
        offered = expected is not None and expected in prompt
        reply = {
            "decision": "MATCH" if offered else "NO_CONFIDENT_MATCH",
            "provider_id": expected if offered else None,
            "confidence": 0.95 if offered else 0.2,
            "evidence_cited": [],
            "reasoning": "scripted oracle",
        }
        return LLMResponse(
            content=json.dumps(reply),
            provider=self.name,
            model=self.model,
            prompt_tokens=400,
            completion_tokens=50,
        )


def _truth(prepared: Any) -> dict[str, str | None]:
    return {
        w.record_id: (w.truth.expected_provider_id if w.truth else None) for w in prepared
    }


def _adjudicator(provider: Any, cache: Any = None) -> tuple[LlmAdjudicator, TokenMeter, Any]:
    router = LLMRouter(
        providers=[provider], cache=cache, sleep=lambda _s: None, rng=random.Random(0), max_attempts=1
    )
    meter = TokenMeter()
    return LlmAdjudicator(router=MeteredRouter(router, meter)), meter, router  # type: ignore[arg-type]


def test_sampling_every_record_reproduces_the_exact_answer(prepared: Any, engine: Any) -> None:
    oracle = OracleProvider(truth=_truth(prepared))
    judge, meter, _ = _adjudicator(oracle)
    payload = run_llm_experiment(
        prepared, engine, judge, meter, "priced", PRICE, seed=SEED, sample_per_stratum=100_000,
        bootstrap=50,
    )
    population = payload["population"]
    assert payload["sample"]["grey"] == population["grey"] > 0
    assert payload["sample"]["decided"] == population["decided"]

    exact_judge, _, _ = _adjudicator(OracleProvider(truth=_truth(prepared)))
    exact = evaluate(
        prepared, ProbabilisticLlmStrategy(engine, exact_judge), show_progress=False
    ).overall
    routed = payload["strategies"]["routed"]
    assert routed["true_positives"] == pytest.approx(exact.true_positives)
    assert routed["false_positives"] == pytest.approx(exact.false_positives)
    assert routed["f1"] == pytest.approx(exact.f1, abs=1e-6)

    unassisted = payload["strategies"]["probabilistic"]
    assert unassisted["exact"] is True
    # An oracle can only help: every grey-band record it answers is right.
    assert routed["recall"] >= unassisted["recall"]


def test_the_baseline_costs_more_calls_and_the_saving_is_consistent(
    prepared: Any, engine: Any
) -> None:
    judge, meter, _ = _adjudicator(OracleProvider(truth=_truth(prepared)))
    payload = run_llm_experiment(
        prepared, engine, judge, meter, "priced", PRICE, seed=SEED, sample_per_stratum=40,
        bootstrap=200,
    )
    cost = payload["cost"]
    population = payload["population"]
    assert cost["routed"]["calls"] == population["grey"]
    assert cost["everything"]["calls"] == population["grey"] + population["decided"]
    assert cost["everything"]["usd"] > cost["routed"]["usd"] > 0
    assert cost["saving"]["calls"] == pytest.approx(
        1 - population["grey"] / (population["grey"] + population["decided"]), abs=1e-6
    )
    # Every scripted call used 400 + 50 tokens, so the per-call means are exact.
    assert cost["tokens_per_call"]["grey"] == {"prompt": 400.0, "completion": 50.0}

    for name in ("routed", "everything"):
        row = payload["strategies"][name]
        lo, hi = row["interval"]["f1"]
        assert lo <= row["f1"] <= hi, name


def test_tokens_are_metered_when_the_answer_comes_from_the_cache(
    prepared: Any, engine: Any, tmp_path: Any
) -> None:
    cache = FileCache(tmp_path / "llm")
    first_judge, first_meter, _ = _adjudicator(OracleProvider(truth=_truth(prepared)), cache)
    first = run_llm_experiment(
        prepared, engine, first_judge, first_meter, "priced", PRICE, seed=SEED,
        sample_per_stratum=15, bootstrap=10,
    )
    replay_oracle = OracleProvider(truth=_truth(prepared))
    again_judge, again_meter, _ = _adjudicator(replay_oracle, cache)
    again = run_llm_experiment(
        prepared, engine, again_judge, again_meter, "priced", PRICE, seed=SEED,
        sample_per_stratum=15, bootstrap=10,
    )
    assert replay_oracle.calls == 0
    assert again["sample"]["live_calls"] == 0
    assert again["sample"]["cache_hits"] == first["sample"]["live_calls"] > 0
    assert again["cost"] == first["cost"]
    assert again["strategies"] == first["strategies"]


def test_a_call_that_never_completed_is_dropped_and_reported(
    prepared: Any, engine: Any
) -> None:
    oracle = OracleProvider(truth=_truth(prepared), fail_every=3)
    judge, meter, _ = _adjudicator(oracle)
    payload = run_llm_experiment(
        prepared, engine, judge, meter, "priced", PRICE, seed=SEED, sample_per_stratum=30,
        bootstrap=10,
    )
    sample = payload["sample"]
    assert sample["failed"] > 0
    assert sample["grey"] + sample["decided"] + sample["failed"] == min(
        30, payload["population"]["grey"]
    ) + min(30, payload["population"]["decided"])
    assert any("never completed" in note for note in payload["notes"])


def test_a_quota_that_runs_out_leaves_a_random_subsample_of_both_strata(
    prepared: Any, engine: Any
) -> None:
    """The regression behind a real run reporting F1 1.05.

    The synthetic file is ordered by scenario, matches first. Calling the sample
    in file order, grey band first, and losing the tail to a quota kept the
    matches and the grey band and dropped the rest, so the scaled estimate
    found more true positives than there are matches.
    """
    oracle = OracleProvider(truth=_truth(prepared), quota=70)
    judge, meter, _ = _adjudicator(oracle)
    payload = run_llm_experiment(
        prepared, engine, judge, meter, "priced", PRICE, seed=SEED, sample_per_stratum=100,
        bootstrap=50,
    )
    sample = payload["sample"]
    assert sample["failed"] > 0
    # Interleaved: the cut takes from both strata, not the whole of the second.
    assert sample["grey"] > 0
    assert sample["decided"] > 0
    # Random order: the answered records are not a prefix of the file.
    order = {w.record_id: i for i, w in enumerate(prepared)}
    answered = [order[r] for r in oracle.seen]
    assert answered != sorted(answered)
    for name, row in payload["strategies"].items():
        assert 0.0 <= row["recall"] <= 1.0, name
        assert 0.0 <= row["f1"] <= 1.0, name


def test_a_stratum_with_too_few_answers_is_withheld_not_extrapolated(
    prepared: Any, engine: Any
) -> None:
    judge, meter, _ = _adjudicator(OracleProvider(truth=_truth(prepared), quota=6))
    payload = run_llm_experiment(
        prepared, engine, judge, meter, "priced", PRICE, seed=SEED, sample_per_stratum=60,
        bootstrap=10,
    )
    assert set(payload["sample"]["withheld"]) == {"routed", "everything"}
    assert set(payload["strategies"]) == {"probabilistic"}
    assert payload["strategies"]["probabilistic"]["exact"] is True
    assert any("too few calls answered" in note for note in payload["notes"])
    # Calls are counted from stratum sizes, not the sample, so cost survives.
    assert payload["cost"]["routed"]["calls"] == payload["population"]["grey"]


# --------------------------------------------------------------------------
# the service, against the database
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lab_settings(owner_url: str, tmp_path_factory: Any) -> Iterator[Any]:
    from concordance.config import Settings
    from concordance.db.session import dispose_engine

    dispose_engine()
    root = tmp_path_factory.mktemp("lab-data")
    yield Settings(
        DATABASE_URL=owner_url,
        DB_CONNECT_TIMEOUT=20,
        DATA_DIR=root,
        LLM_ENABLED=True,
        GROQ_API_KEY="unused",
        LLM_PROVIDER_CHAIN="groq",
        LLM_CACHE_DIR=root / "cache",
        LAB_SWEEP_WORKERS=1,
    )
    dispose_engine()


@pytest.fixture(scope="module")
def lab_rows(lab_settings: Any) -> Iterator[list[uuid.UUID]]:
    """Ids the tests create; deleted afterwards along with their jobs and audit rows."""
    from sqlalchemy import delete, select

    from concordance.db.models import AuditLog, Job, LabSweep
    from concordance.db.session import session_scope

    with session_scope(lab_settings) as session:
        if session.scalar(select(LabSweep).where(LabSweep.status.in_(("QUEUED", "RUNNING")))):
            pytest.skip("a real Lab experiment is live in this database")

    created: list[uuid.UUID] = []
    yield created
    with session_scope(lab_settings) as session:
        jobs = [
            j
            for (j,) in session.execute(select(LabSweep.job_id).where(LabSweep.id.in_(created)))
            if j is not None
        ]
        session.execute(delete(AuditLog).where(AuditLog.entity_id.in_([str(i) for i in created])))
        session.execute(delete(LabSweep).where(LabSweep.id.in_(created)))
        if jobs:
            session.execute(delete(Job).where(Job.id.in_(jobs)))


def _results(session: Any, settings: Any, sweep_id: uuid.UUID) -> Any:
    from concordance.api import schemas
    from concordance.api.routers.lab import results

    out = results(session=session, settings=settings, _user=None, sweep_id=sweep_id)  # type: ignore[arg-type]
    return schemas.LabResultsOut.model_validate(out.model_dump())


def test_a_sweep_is_queued_refused_twice_run_and_read_back(
    lab_settings: Any, lab_rows: list[uuid.UUID]
) -> None:
    from concordance.audit.service import Actor
    from concordance.db.session import session_scope
    from concordance.errors import ConflictError
    from concordance.lab import service

    with session_scope(lab_settings) as session:
        row = service.request_sweep(
            session, lab_settings, Actor.system(), levels=[0.6, 0.2],
            providers=PROVIDERS, sanctions=SANCTIONS,
        )
        lab_rows.append(row.id)
        sweep_id = row.id
        assert row.status == "QUEUED" and row.job_id is not None
        assert row.params["levels"] == [0.2, 0.6]

    with session_scope(lab_settings) as session, pytest.raises(ConflictError):
        service.request_sweep(session, lab_settings, Actor.system())

    with session_scope(lab_settings) as session:
        summary = service.run_sweep(session, lab_settings, sweep_id)
    assert summary["cells"] == 2 * len(service.SWEEP_STRATEGIES)

    with session_scope(lab_settings) as session:
        read = _results(session, lab_settings, sweep_id)
    assert read.sweep is not None and read.sweep.status == "COMPLETED"
    assert read.sweep.progress == {"done": 2, "total": 2}
    assert {(c.level, c.strategy) for c in read.cells} == {
        (level, s) for level in (0.2, 0.6) for s in service.SWEEP_STRATEGIES
    }
    probabilistic = [c for c in read.cells if c.strategy == "probabilistic"]
    assert all(c.scenarios and c.grey_band_fraction is not None for c in probabilistic)
    models = {(c.level, c.model) for c in read.calibration}
    assert (0.2, "individual") in models and (0.6, "individual") in models
    for block in read.calibration:
        assert block.before.bins and block.after.bins
        assert block.t_auto_reject is not None and block.t_auto_accept is not None


def test_an_llm_run_extends_the_sweep(
    lab_settings: Any, lab_rows: list[uuid.UUID], prepared: Any, monkeypatch: Any
) -> None:
    from sqlalchemy import select

    from concordance.audit.service import Actor
    from concordance.db.models import LabSweep
    from concordance.db.session import session_scope
    from concordance.errors import InvalidError
    from concordance.lab import service

    with session_scope(lab_settings) as session:
        sweep = session.scalar(select(LabSweep).where(LabSweep.id.in_(lab_rows), LabSweep.kind == "sweep"))
        if sweep is None or sweep.status != "COMPLETED":
            pytest.skip("the sweep test did not leave a completed sweep")
        sweep_id = sweep.id
        with pytest.raises(InvalidError):
            service.request_llm(session, lab_settings, Actor.system(), sweep_id=sweep_id, levels=[0.4])

    # The sweep's own dataset at 0.6, generated under the same seed, is what
    # the run reads; the oracle only needs record ids to look up the truth.
    truth_dataset = prepare(
        lab_settings.DATA_DIR / "sweep" / "sweep-c0.6", max_candidates=50, show_progress=False
    )
    oracle = OracleProvider(truth=_truth(truth_dataset))
    monkeypatch.setattr(
        LLMRouter,
        "from_settings",
        classmethod(
            lambda cls, settings, cache=None, **_: cls(
                providers=[oracle], cache=cache, sleep=lambda _s: None, rng=random.Random(0)
            )
        ),
    )

    with session_scope(lab_settings) as session:
        row = service.request_llm(
            session, lab_settings, Actor.system(), sweep_id=sweep_id, levels=[0.6], sample=20
        )
        lab_rows.append(row.id)
        llm_id = row.id

    with session_scope(lab_settings) as session:
        assert service.run_llm(session, lab_settings, llm_id) == {"lab_id": str(llm_id), "levels": 1}

    with session_scope(lab_settings) as session:
        read = _results(session, lab_settings, sweep_id)
    assert read.llm_run is not None and read.llm_run.id == llm_id
    assert read.llm_run.status == "COMPLETED"
    (level,) = read.llm
    assert level.level == 0.6
    assert level.sample.grey + level.sample.decided > 0
    assert set(level.strategies) == {"probabilistic", "routed", "everything"}
    assert level.cost.everything.calls > level.cost.routed.calls
    assert oracle.calls > 0


def test_a_live_experiment_whose_job_died_reads_as_failed(
    lab_settings: Any, lab_rows: list[uuid.UUID]
) -> None:
    from concordance.audit.service import Actor
    from concordance.db.models import Job
    from concordance.db.session import session_scope
    from concordance.lab import service

    with session_scope(lab_settings) as session:
        row = service.request_sweep(session, lab_settings, Actor.system(), levels=[0.1])
        lab_rows.append(row.id)
        job = session.get(Job, row.job_id)
        assert job is not None
        job.status = "DEAD"
        job.last_error = "worker killed"
        session.flush()
        assert service.effective_status(session, row) == ("FAILED", "worker killed")
        row.status = "FAILED"
