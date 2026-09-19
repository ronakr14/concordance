"""Command line entry point.

Command groups mirror the stages: ``data`` (Stage 1), ``match`` (Stages 2-3),
``llm`` (Stage 4), ``db`` (Stage 5), ``report`` (Stage 3 onward). Commands that
belong to a stage not yet built are registered anyway and exit with a message
naming the stage, so the surface of the finished system is visible from day one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from concordance import __version__
from concordance.config import Settings, get_settings
from concordance.logging_setup import configure_logging, get_logger, new_correlation_id

app = typer.Typer(
    name="concordance",
    help="Provider sanctions and exclusions reconciliation.",
    no_args_is_help=True,
    add_completion=False,
)
data_app = typer.Typer(
    help="Synthetic data generation and dataset inspection.", no_args_is_help=True
)
match_app = typer.Typer(help="Normalization, blocking, scoring, calibration.", no_args_is_help=True)
llm_app = typer.Typer(help="LLM router, cache and grey-band adjudication.", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations and loaders.", no_args_is_help=True)
report_app = typer.Typer(help="Evaluation reports and sweeps.", no_args_is_help=True)
run_app = typer.Typer(
    help="Reconciliation runs: start, replay, diff, inspect.", no_args_is_help=True
)
jobs_app = typer.Typer(help="The job queue and the worker.", no_args_is_help=True)
api_app = typer.Typer(help="The HTTP API: serve it, or export its contract.", no_args_is_help=True)
lab_app = typer.Typer(
    help="The Lab: robustness sweeps and the LLM cost experiment, stored for the UI.",
    no_args_is_help=True,
)

app.add_typer(data_app, name="data")
app.add_typer(match_app, name="match")
app.add_typer(llm_app, name="llm")
app.add_typer(api_app, name="api")
app.add_typer(db_app, name="db")
app.add_typer(report_app, name="report")
app.add_typer(run_app, name="run")
app.add_typer(jobs_app, name="jobs")
app.add_typer(lab_app, name="lab")

log = get_logger("cli")

SeedOpt = Annotated[
    int | None, typer.Option("--seed", "-s", help="Random seed; defaults to RANDOM_SEED.")
]


def _not_until(stage: int, what: str) -> None:
    typer.secho(f"{what} arrives at Stage {stage}. Not built yet - see docs/PLAN.md.", fg="yellow")
    raise typer.Exit(code=2)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"concordance {__version__}")
        raise typer.Exit()


def start(seed: int | None = None, echo_config: bool = True) -> tuple[Settings, int]:
    """Common preamble: settings, logging, correlation id, resolved-config echo.

    Every command calls this, so every command run is reproducible from its own
    log output alone - the seed and the config it actually ran with are printed,
    not assumed.
    """
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, json_logs=settings.ENV == "production")
    cid = new_correlation_id()
    resolved_seed = settings.RANDOM_SEED if seed is None else seed
    if echo_config:
        log.info("run.config", cid=cid, seed=resolved_seed, **settings.public_dict())
    return settings, resolved_seed


@app.callback()
def main_callback(
    _version: Annotated[
        bool | None,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
        ),
    ] = None,
) -> None:
    """Concordance CLI."""


# --------------------------------------------------------------------------
# data - Stage 1
# --------------------------------------------------------------------------


@data_app.command("seed")
def data_seed(
    providers: Annotated[int, typer.Option("--providers", help="Provider master size.")] = 50_000,
    sanctions: Annotated[int, typer.Option("--sanctions", help="Sanction record count.")] = 5_000,
    corruption: Annotated[
        float, typer.Option("--corruption", min=0.0, max=0.9, help="Corruption dial.")
    ] = 0.5,
    seed: SeedOpt = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Output directory; defaults to DATA_DIR/generated.")
    ] = None,
    excel: Annotated[
        bool, typer.Option("--excel/--no-excel", help="Also write the sanction Excel export.")
    ] = True,
) -> None:
    """Generate the synthetic dataset, its ground truth and the Excel export."""
    from concordance.synth.pipeline import seed_dataset

    settings, resolved_seed = start(seed)
    manifest = seed_dataset(
        providers=providers,
        sanctions=sanctions,
        corruption=corruption,
        seed=resolved_seed,
        out_dir=out or settings.generated_dir,
        write_excel=excel,
    )
    typer.echo(
        f"providers={manifest['counts']['providers']} "
        f"sanctions={manifest['counts']['sanction_records']} "
        f"corruption={corruption} seed={resolved_seed}\n"
        f"content_hash={manifest['content_hash']}\n"
        f"written to {manifest['out_dir']}"
    )


@data_app.command("inspect")
def data_inspect(
    out: Annotated[Path | None, typer.Option("--out", help="Dataset directory.")] = None,
    scenario: Annotated[
        str | None, typer.Option("--scenario", help="Filter to one scenario tag.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit")] = 10,
    seed: SeedOpt = None,
) -> None:
    """Print a sample of generated records beside their ground truth."""
    from concordance.synth.inspect import inspect_dataset

    settings, _ = start(seed, echo_config=False)
    for line in inspect_dataset(out or settings.generated_dir, scenario=scenario, limit=limit):
        typer.echo(line)


@data_app.command("verify")
def data_verify(
    out: Annotated[Path | None, typer.Option("--out", help="Dataset directory.")] = None,
    seed: SeedOpt = None,
) -> None:
    """Check dataset invariants: one ground-truth row per record, hashes, counts."""
    from concordance.synth.inspect import verify_dataset

    settings, _ = start(seed, echo_config=False)
    report = verify_dataset(out or settings.generated_dir)
    for line in report.lines:
        typer.echo(line)
    raise typer.Exit(code=0 if report.ok else 1)


# --------------------------------------------------------------------------
# match - Stages 2 and 3
# --------------------------------------------------------------------------


@match_app.command("normalize")
def match_normalize(
    out: Annotated[Path | None, typer.Option("--out", help="Dataset directory.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Records to show.")] = 10,
    seed: SeedOpt = None,
) -> None:
    """Show how records normalize - both sides of a pair, side by side."""
    from concordance.matching.normalization import normalize_provider, normalize_sanction
    from concordance.store.parquet_store import ParquetRecordStore

    settings, _ = start(seed, echo_config=False)
    store = ParquetRecordStore(out or settings.generated_dir)
    truth = store.ground_truth()
    for record in store.sanction_batch(0, limit):
        norm = normalize_sanction(record)
        typer.echo(f"--- {record.record_id}  org={norm.is_organization}  npi={norm.npi_status}")
        typer.echo(
            f"  sanction name={norm.name_norm!r} sorted={norm.name_sorted_norm!r} "
            f"org={norm.org_name_norm!r} phonetic={norm.phonetic_keys} dob={norm.dob}"
        )
        typer.echo(
            f"  sanction addr={norm.address.line!r} unit={norm.address.unit!r} "
            f"{norm.address.city} {norm.address.state} {norm.address.zip5} lic={norm.license_number}"
        )
        gt = truth.get(record.record_id)
        provider = (
            store.get_provider(gt.expected_provider_id) if gt and gt.expected_provider_id else None
        )
        if provider is not None:
            pnorm = normalize_provider(provider)
            typer.echo(
                f"  provider name={pnorm.name_norm!r} sorted={pnorm.name_sorted_norm!r} "
                f"org={pnorm.org_name_norm!r} phonetic={pnorm.phonetic_keys} dob={pnorm.dob}"
            )
            typer.echo(
                f"  provider addr={pnorm.address.line!r} unit={pnorm.address.unit!r} "
                f"{pnorm.address.city} {pnorm.address.state} {pnorm.address.zip5} lic={pnorm.license_number}"
            )


@match_app.command("block")
def match_block(
    out: Annotated[Path | None, typer.Option("--out", help="Dataset directory.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Records to show.")] = 5,
    max_candidates: Annotated[int | None, typer.Option("--max-candidates")] = None,
    seed: SeedOpt = None,
) -> None:
    """Build the blocking index and show the candidates for a few records."""
    from concordance.matching.blocking import InMemoryCandidateGenerator
    from concordance.store.parquet_store import ParquetRecordStore

    settings, _ = start(seed, echo_config=False)
    store = ParquetRecordStore(out or settings.generated_dir)
    generator = InMemoryCandidateGenerator(
        max_candidates=max_candidates or settings.MAX_CANDIDATES_PER_RECORD
    )
    generator.build(store)
    typer.echo(f"index: {generator.stats.as_dict()}")
    truth = store.ground_truth()
    for record in store.sanction_batch(0, limit):
        candidates = generator.candidates(record)
        gt = truth.get(record.record_id)
        expected = gt.expected_provider_id if gt else None
        typer.echo(f"--- {record.record_id}  expected={expected}  candidates={len(candidates)}")
        for candidate in candidates[:8]:
            mark = "*" if candidate.provider_id == expected else " "
            typer.echo(f"  {mark} {candidate.provider_id}  via {list(candidate.blocking_keys)}")


@match_app.command("blocking-recall")
def match_blocking_recall(
    out: Annotated[Path | None, typer.Option("--out", help="Dataset directory.")] = None,
    corruption: Annotated[
        float | None, typer.Option("--corruption", help="Reseed at this level first.")
    ] = None,
    max_candidates: Annotated[int | None, typer.Option("--max-candidates")] = None,
    trigram_floor: Annotated[float, typer.Option("--trigram-floor")] = 0.3,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Evaluate only the first N records.")
    ] = None,
    memory: Annotated[
        bool,
        typer.Option(
            "--memory/--no-memory", help="Measure the index footprint (inflates build time)."
        ),
    ] = False,
    report: Annotated[
        Path | None, typer.Option("--report", help="Also write the JSON report here.")
    ] = None,
    seed: SeedOpt = None,
) -> None:
    """Measure blocking recall - the ceiling on system recall."""
    from concordance.eval.blocking_recall import measure_blocking_recall, write_report

    settings, resolved_seed = start(seed, echo_config=False)
    dataset = out or settings.generated_dir

    if corruption is not None:
        # A sweep asks for a specific corruption level; generate it rather than
        # measure whatever happens to be on disk.
        from concordance.synth.pipeline import seed_dataset

        dataset = dataset if out else settings.DATA_DIR / f"blocking-{corruption}"
        seed_dataset(
            providers=50_000,
            sanctions=5_000,
            corruption=corruption,
            seed=resolved_seed,
            out_dir=dataset,
            write_excel=False,
        )

    result = measure_blocking_recall(
        dataset,
        max_candidates=max_candidates or settings.MAX_CANDIDATES_PER_RECORD,
        trigram_floor=trigram_floor,
        limit=limit,
        measure_memory=memory,
    )
    for line in result.lines():
        typer.echo(line)
    path = report or (settings.REPORTS_DIR / f"blocking_recall_{result.corruption_level}.json")
    typer.echo(f"\nJSON report: {write_report(result, path)}")


@match_app.command("fit")
def match_fit(
    out: Annotated[Path | None, typer.Option("--out", help="Dataset directory.")] = None,
    corruption: Annotated[
        float | None,
        typer.Option("--corruption", min=0.0, max=0.9, help="Reseed at this level first."),
    ] = None,
    target_precision: Annotated[
        float | None, typer.Option("--target-precision", min=0.0, max=1.0)
    ] = None,
    restarts: Annotated[int, typer.Option("--restarts", min=1, help="Seeded EM restarts.")] = 5,
    holdout: Annotated[
        float, typer.Option("--holdout", min=0.05, max=0.9, help="Calibration holdout fraction.")
    ] = 0.4,
    margin_delta: Annotated[float, typer.Option("--margin-delta", min=0.0, max=1.0)] = 0.05,
    max_candidates: Annotated[int | None, typer.Option("--max-candidates")] = None,
    config_out: Annotated[
        Path | None, typer.Option("--config-out", help="Where to write the config JSON.")
    ] = None,
    seed: SeedOpt = None,
) -> None:
    """Fit Fellegi-Sunter m/u parameters by EM, calibrate, choose thresholds."""
    from concordance.eval.fitting import config_filename, fit_config
    from concordance.eval.pairs import prepare

    settings, resolved_seed = start(seed, echo_config=False)
    dataset = _dataset_for(settings, out, corruption, resolved_seed)
    prepared = prepare(dataset, max_candidates=max_candidates or settings.MAX_CANDIDATES_PER_RECORD)
    report = fit_config(
        prepared,
        seed=resolved_seed,
        target_precision=target_precision or settings.TARGET_PRECISION,
        restarts=restarts,
        holdout_fraction=holdout,
        margin_delta=margin_delta,
    )
    for line in report.lines():
        typer.echo(line)
    path = config_out or (
        settings.DATA_DIR
        / "configs"
        / config_filename(prepared.corruption_level or 0.0, resolved_seed)
    )
    typer.echo(f"\nconfig: {report.config.write(path)}")


@match_app.command("run")
def match_run(
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Fitted config JSON; one is fitted on the fly if omitted."),
    ] = None,
    config_id: Annotated[
        str | None, typer.Option("--config-id", help="Config id under DATA_DIR/configs.")
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Dataset directory.")] = None,
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy", help="deterministic | fuzzy | probabilistic | probabilistic_llm"
        ),
    ] = "probabilistic",
    limit: Annotated[int, typer.Option("--limit", help="Records to show.")] = 10,
    max_candidates: Annotated[int | None, typer.Option("--max-candidates")] = None,
    seed: SeedOpt = None,
) -> None:
    """Score sanction records and print each decision with the evidence behind it."""
    from concordance.eval.pairs import prepare
    from concordance.matching.strategies import build_strategy

    settings, resolved_seed = start(seed, echo_config=False)
    prepared = prepare(
        out or settings.generated_dir,
        max_candidates=max_candidates or settings.MAX_CANDIDATES_PER_RECORD,
        limit=limit,
    )
    engine = _engine_for(
        settings, _config_path(settings, config, config_id), prepared, resolved_seed
    )
    chosen = build_strategy(strategy, engine, _adjudicator_for(settings, strategy))
    for work in prepared:
        result = chosen.decide(work.normalized, work.candidates)
        expected = work.truth.expected_provider_id if work.truth else None
        typer.echo(
            f"--- {result.record_id}  {result.decision}  via {result.route}/{result.reason}  "
            f"conf={result.confidence:.4f}  expected={expected}"
        )
        for candidate in result.candidates[:3]:
            mark = "*" if candidate.provider_id == expected else " "
            typer.echo(
                f"  {mark} {candidate.provider_id}  conf={candidate.confidence:.4f}  "
                f"w={candidate.match_weight:+.2f}  {candidate.levels}"
            )
        for note in result.notes:
            typer.echo(f"    note: {note}")


@match_app.command("calibrate")
def match_calibrate(
    config: Annotated[Path | None, typer.Option("--config", help="Fitted config JSON.")] = None,
    config_id: Annotated[str | None, typer.Option("--config-id", help="Config id under DATA_DIR/configs.")] = None,
    seed: SeedOpt = None,
) -> None:
    """Show the calibration curve and thresholds of a fitted config."""
    from concordance.matching.scoring_config import ScoringConfig

    settings, _ = start(seed, echo_config=False)
    path = _config_path(settings, config, config_id) or _latest_config(settings)
    if path is None:
        typer.secho("No config found - run 'concordance match fit' first.", fg="red")
        raise typer.Exit(code=1)
    loaded = ScoringConfig.read(path)
    typer.echo(f"config {loaded.config_id}  fitted {loaded.fitted_at}  seed {loaded.seed}")
    for kind, bundle in sorted(loaded.bundles.items()):
        calibration = bundle.calibration
        before = calibration.get("before", {})
        after = calibration.get("after", {})
        typer.echo(f"\n{kind}")
        typer.echo(
            f"  ECE   {before.get('ece', 0):.4f} -> {after.get('ece', 0):.4f}"
            f"    Brier {before.get('brier', 0):.4f} -> {after.get('brier', 0):.4f}"
        )
        typer.echo(f"  thresholds {bundle.thresholds.as_dict()}")
        typer.echo("  reliability bins (after calibration):")
        for row in after.get("bins", []):
            if row["count"]:
                typer.echo(
                    f"    [{row['lower']:.1f},{row['upper']:.1f})  n={row['count']:<6} "
                    f"predicted={row['mean_predicted']:.4f}  "
                    f"observed={row['observed_frequency']:.4f}"
                )


def _sweep(
    levels: str | None,
    strategies: str | None,
    providers: int,
    sanctions: int,
    workers: int | None,
    reuse: bool,
    report: Path | None,
    seed: int | None,
) -> None:
    """The sweep itself. Two commands front it, so it lives in one place."""
    from concordance.eval.sweep import DEFAULT_LEVELS, DEFAULT_WORKERS, sweep, write_sweep
    from concordance.matching.strategies import ALL_STRATEGIES, StrategyName

    settings, resolved_seed = start(seed, echo_config=False)
    chosen_levels = tuple(float(v) for v in levels.split(",")) if levels else DEFAULT_LEVELS
    chosen_strategies = (
        tuple(StrategyName(v.strip()) for v in strategies.split(","))
        if strategies
        else ALL_STRATEGIES
    )
    result = sweep(
        root=settings.DATA_DIR / "sweep",
        seed=resolved_seed,
        levels=chosen_levels,
        strategies=chosen_strategies,
        providers=providers,
        sanctions=sanctions,
        max_candidates=settings.MAX_CANDIDATES_PER_RECORD,
        target_precision=settings.TARGET_PRECISION,
        workers=workers or DEFAULT_WORKERS,
        reuse=reuse,
    )
    for line in result.lines():
        typer.echo(line)
    path = report or (settings.REPORTS_DIR / "sweep.json")
    typer.echo(f"\nJSON: {write_sweep(result, path)}")
    raise typer.Exit(code=1 if result.errors else 0)


@match_app.command("sweep")
def match_sweep(
    levels: Annotated[
        str | None, typer.Option("--levels", help="Comma-separated corruption levels.")
    ] = None,
    strategies: Annotated[
        str | None, typer.Option("--strategies", help="Comma-separated strategy names.")
    ] = None,
    providers: Annotated[int, typer.Option("--providers")] = 50_000,
    sanctions: Annotated[int, typer.Option("--sanctions")] = 5_000,
    workers: Annotated[
        int | None,
        typer.Option("--workers", help="Levels in parallel; memory-bound, not CPU-bound."),
    ] = None,
    reuse: Annotated[
        bool, typer.Option("--reuse/--regenerate", help="Reuse datasets already on disk.")
    ] = True,
    report: Annotated[
        Path | None, typer.Option("--report", help="Where to write the sweep JSON.")
    ] = None,
    seed: SeedOpt = None,
) -> None:
    """Sweep corruption levels against every strategy and write the robustness curve."""
    _sweep(levels, strategies, providers, sanctions, workers, reuse, report, seed)


# --------------------------------------------------------------------------
# llm - Stage 4
# --------------------------------------------------------------------------


@llm_app.command("ping")
def llm_ping(seed: SeedOpt = None) -> None:
    """Check each configured provider in the fallback chain."""
    from concordance.llm.router import LLMRouter, build_providers

    settings, _ = start(seed, echo_config=False)
    providers = build_providers(settings)
    if not providers:
        typer.secho(
            f"No provider in chain {settings.LLM_PROVIDER_CHAIN!r} has an API key. "
            "Set GROQ_API_KEY or OPENROUTER_API_KEY in .env.",
            fg="red",
        )
        raise typer.Exit(code=1)
    if not settings.LLM_ENABLED:
        typer.secho(
            "LLM_ENABLED=false - pinging anyway, but adjudication will abstain.", fg="yellow"
        )

    router = LLMRouter(providers=providers, cache=None, enabled=True)
    failures = 0
    try:
        for row in router.ping():
            if row["ok"]:
                typer.secho(
                    f"  ok    {row['provider']:<12} {row['model']:<44} "
                    f"{row['latency_ms']:>5}ms  {row['tokens']:>4} tok  {row['content']!r}",
                    fg="green",
                )
            else:
                failures += 1
                typer.secho(
                    f"  FAIL  {row['provider']:<12} {row['model']:<44} "
                    f"{row['error_type']}: {row['error']}",
                    fg="red",
                )
    finally:
        router.close()
    raise typer.Exit(code=1 if failures else 0)


@llm_app.command("adjudicate")
def llm_adjudicate(
    config: Annotated[
        Path | None, typer.Option("--config", help="Fitted config JSON.")
    ] = None,
    config_id: Annotated[str | None, typer.Option("--config-id")] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Dataset directory.")] = None,
    limit: Annotated[
        int, typer.Option("--limit", help="Grey-band records to adjudicate.")
    ] = 5,
    scan: Annotated[
        int, typer.Option("--scan", help="Records to score while looking for grey-band ones.")
    ] = 500,
    max_candidates: Annotated[int | None, typer.Option("--max-candidates")] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Bypass the cache; every pair hits the network.")
    ] = False,
    seed: SeedOpt = None,
) -> None:
    """Send grey-band pairs to the adjudicator and print each decision with its evidence."""
    from concordance.domain import Outcome
    from concordance.eval.pairs import prepare
    from concordance.llm.ai_matcher import LlmAdjudicator
    from concordance.llm.cache import FileCache, NullCache
    from concordance.llm.router import LLMRouter
    from concordance.matching.adjudication import AdjudicationRequest

    settings, resolved_seed = start(seed, echo_config=False)
    if not settings.LLM_ENABLED:
        typer.secho(
            "LLM_ENABLED=false. Set it true in .env to adjudicate; "
            "the rest of the pipeline runs without it.",
            fg="red",
        )
        raise typer.Exit(code=1)

    prepared = prepare(
        out or settings.generated_dir,
        max_candidates=max_candidates or settings.MAX_CANDIDATES_PER_RECORD,
        limit=scan,
    )
    engine = _engine_for(
        settings, _config_path(settings, config, config_id), prepared, resolved_seed
    )
    cache = NullCache() if no_cache else FileCache(settings.llm_cache_dir)
    router = LLMRouter.from_settings(settings, cache=cache)
    if not router.available:
        typer.secho(f"No usable provider in chain {router.chain_names()}.", fg="red")
        raise typer.Exit(code=1)
    adjudicator = LlmAdjudicator(router=router, top_k=settings.LLM_TOP_K)
    typer.echo(f"chain: {router.chain_names()}  prompt: {adjudicator.prompt_version}\n")

    seen = 0
    try:
        for work in prepared:
            if seen >= limit:
                break
            result = engine.score_record(work.normalized, work.candidates)
            if result.decision is not Outcome.AMBIGUOUS or not result.candidates:
                continue
            seen += 1
            thresholds = engine.bundle(result.kind).thresholds
            request = AdjudicationRequest.of(
                result,
                (thresholds.t_auto_reject, thresholds.t_auto_accept),
                top_k=settings.LLM_TOP_K,
            )
            outcome = adjudicator.adjudicate(request)
            expected = work.truth.expected_provider_id if work.truth else None
            verdict = "abstained" if outcome.abstained else str(outcome.decision)
            typer.echo(
                f"--- {result.record_id}  engine={result.decision}/{result.reason} "
                f"conf={result.confidence:.4f}  ->  {verdict}  expected={expected}"
            )
            if not outcome.abstained:
                mark = "*" if outcome.provider_id == expected else " "
                typer.echo(
                    f"  {mark} provider={outcome.provider_id}  "
                    f"model_confidence={outcome.confidence:.3f}  "
                    f"tokens={outcome.tokens}  cost=${outcome.cost_usd:.6f}"
                )
                typer.echo(f"    cited: {list(outcome.evidence_cited)}")
            typer.echo(f"    {outcome.reasoning}")
    finally:
        router.close()

    if seen == 0:
        typer.secho(
            f"No grey-band records in the first {scan}. Raise --scan or refit.", fg="yellow"
        )
    stats = adjudicator.stats()
    typer.echo(
        f"\nadjudicated={stats['adjudicated']}  abstained={stats['abstentions']}  "
        f"repairs={stats['repairs']}  rejections={stats['rejections']}"
    )
    router_stats = stats["router"]
    typer.echo(
        f"calls={router_stats['calls']}  cache_hits={router_stats['cache_hits']}  "
        f"retries={router_stats['retries']}  failovers={router_stats['failovers']}  "
        f"tokens={stats['tokens']}  cost=${stats['cost_usd']:.6f}"
    )


@llm_app.command("cache-stats")
def llm_cache_stats(seed: SeedOpt = None) -> None:
    """Show LLM response cache statistics."""
    from collections import Counter

    from concordance.llm.cache import FileCache

    settings, _ = start(seed, echo_config=False)
    cache = FileCache(settings.llm_cache_dir)
    entries = cache.entries()
    typer.echo(f"cache: {cache.root}")
    typer.echo(f"entries: {len(entries)}  size: {cache.size_bytes() / 1024:.1f} KiB")
    if not entries:
        return
    by_model = Counter(f"{e.get('provider')}/{e.get('model')}" for e in entries)
    by_prompt = Counter(str(e.get("prompt_version")) for e in entries)
    tokens = sum(int(e.get("response", {}).get("total_tokens", 0)) for e in entries)
    cost = sum(float(e.get("response", {}).get("cost_usd", 0.0)) for e in entries)
    typer.echo(f"tokens stored: {tokens}  cost stored: ${cost:.6f}")
    for name, count in by_model.most_common():
        typer.echo(f"  {count:>6}  {name}")
    for version, count in sorted(by_prompt.items()):
        typer.echo(f"  {count:>6}  prompt {version}")


@llm_app.command("cache-clear")
def llm_cache_clear(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
    seed: SeedOpt = None,
) -> None:
    """Delete every cached LLM response."""
    from concordance.llm.cache import FileCache

    settings, _ = start(seed, echo_config=False)
    cache = FileCache(settings.llm_cache_dir)
    count = cache.entry_count()
    if count == 0:
        typer.echo(f"cache already empty: {cache.root}")
        return
    if not yes:
        typer.confirm(
            f"Delete {count} cached responses under {cache.root}? "
            "Re-running adjudication will call the providers again.",
            abort=True,
        )
    typer.echo(f"removed {cache.clear()} entries from {cache.root}")


# --------------------------------------------------------------------------
# db - Stage 5
# --------------------------------------------------------------------------


def _alembic_config() -> Any:
    """Alembic's config, addressed absolutely.

    `alembic.ini` lives in `backend/` and names a script location relative to
    itself, so running `concordance db upgrade` from anywhere has to point at
    the file rather than rely on the working directory.
    """
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[2]
    ini = backend / "alembic.ini"
    if not ini.is_file():
        raise RuntimeError(f"alembic.ini not found at {ini}")
    return Config(str(ini))


@db_app.command("ping")
def db_ping(seed: SeedOpt = None) -> None:
    """Check that the configured database answers."""
    from concordance.db.session import ping

    settings, _ = start(seed, echo_config=False)
    if ping(settings):
        typer.secho("ok   database responds", fg="green")
        return
    typer.secho("FAIL database did not respond - see the log line above", fg="red")
    raise typer.Exit(code=1)


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "head",
    seed: SeedOpt = None,
) -> None:
    """Run Alembic migrations up to a revision (default `head`)."""
    from alembic import command

    start(seed, echo_config=False)
    command.upgrade(_alembic_config(), revision)
    typer.secho(f"upgraded to {revision}", fg="green")


@db_app.command("downgrade")
def db_downgrade(
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "-1",
    seed: SeedOpt = None,
) -> None:
    """Roll migrations back. `base` removes the schema entirely."""
    from alembic import command

    start(seed, echo_config=False)
    if revision == "base" and not typer.confirm(
        "downgrade to base drops every table and all data. Continue?"
    ):
        raise typer.Abort()
    command.downgrade(_alembic_config(), revision)
    typer.secho(f"downgraded to {revision}", fg="yellow")


@db_app.command("current")
def db_current(seed: SeedOpt = None) -> None:
    """Show the revision the database is on."""
    from alembic import command

    start(seed, echo_config=False)
    command.current(_alembic_config(), verbose=True)


@db_app.command("load")
def db_load(
    source: Annotated[
        Path | None, typer.Option("--from", help="Dataset directory to import.")
    ] = None,
    append: Annotated[
        bool, typer.Option("--append", help="Keep existing rows instead of truncating first.")
    ] = False,
    seed: SeedOpt = None,
) -> None:
    """Load a generated Parquet dataset into Postgres."""
    from concordance.db.loader import analyze, load_dataset
    from concordance.db.session import get_engine

    settings, _ = start(seed, echo_config=False)
    dataset = source or settings.generated_dir
    engine = get_engine(settings)
    report = load_dataset(engine, dataset, truncate=not append)
    analyze(engine)
    typer.echo(
        f"providers={report.providers}  block_keys={report.block_keys}  "
        f"sanctions={report.sanctions}  ground_truth={report.ground_truth}  "
        f"in {report.seconds:.1f}s"
    )
    for stage, seconds in report.stage_seconds.items():
        typer.echo(f"  {stage:<14}{seconds:6.1f}s")


@db_app.command("reset")
def db_reset(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
    seed: SeedOpt = None,
) -> None:
    """Drop every table and migrate back up. Destroys all data."""
    from alembic import command

    start(seed, echo_config=False)
    if not yes and not typer.confirm(
        "This drops every table in the database and recreates them empty. Continue?"
    ):
        raise typer.Abort()
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    typer.secho("database reset to an empty schema at head", fg="yellow")


@db_app.command("import-cache")
def db_import_cache(
    source: Annotated[
        Path | None, typer.Option("--from", help="FileCache directory. Defaults to .cache/llm/.")
    ] = None,
    seed: SeedOpt = None,
) -> None:
    """Copy Stage 4's file cache into `llm_calls` so the answers are not re-bought."""
    from concordance.db.session import session_scope
    from concordance.store.postgres_cache import import_file_cache

    settings, _ = start(seed, echo_config=False)
    directory = source or settings.llm_cache_dir
    with session_scope(settings) as session:
        counts = import_file_cache(session, directory)
    typer.echo(
        f"imported={counts['imported']}  already_present={counts['skipped']}  "
        f"unreadable={counts['failed']}"
    )


# --------------------------------------------------------------------------
# report - Stage 3 onward
# --------------------------------------------------------------------------


@report_app.command("eval")
def report_eval(
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Fitted config JSON; one is fitted on the fly if omitted."),
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Dataset directory.")] = None,
    strategy: Annotated[str, typer.Option("--strategy")] = "probabilistic",
    corruption: Annotated[
        float | None,
        typer.Option("--corruption", min=0.0, max=0.9, help="Reseed at this level first."),
    ] = None,
    max_candidates: Annotated[int | None, typer.Option("--max-candidates")] = None,
    html_out: Annotated[
        Path | None, typer.Option("--html", help="Where to write the standalone HTML report.")
    ] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Where to write the eval_runs JSON payload.")
    ] = None,
    sweep_json: Annotated[
        Path | None, typer.Option("--sweep", help="Sweep results to embed as the robustness curve.")
    ] = None,
    seed: SeedOpt = None,
) -> None:
    """Evaluate a strategy against ground truth and write the standalone HTML report."""
    from concordance.eval.harness import evaluate, write_report
    from concordance.eval.pairs import prepare
    from concordance.eval.report_html import load_sweep, write_html
    from concordance.matching.strategies import build_strategy

    settings, resolved_seed = start(seed, echo_config=False)
    dataset = _dataset_for(settings, out, corruption, resolved_seed)
    prepared = prepare(dataset, max_candidates=max_candidates or settings.MAX_CANDIDATES_PER_RECORD)
    engine, config_payload, config_id = _engine_and_config(
        settings, config, prepared, resolved_seed
    )
    report = evaluate(
        prepared,
        build_strategy(strategy, engine, _adjudicator_for(settings, strategy)),
        config_id=config_id,
        seed=resolved_seed,
    )
    for line in report.lines():
        typer.echo(line)

    level = prepared.corruption_level if prepared.corruption_level is not None else 0.0
    stem = f"eval_{strategy}_{level:.1f}"
    json_path = write_report(report, json_out or settings.REPORTS_DIR / f"{stem}.json")
    sweep_rows = load_sweep(sweep_json or settings.REPORTS_DIR / "sweep.json")
    html_path = write_html(
        report, html_out or settings.REPORTS_DIR / f"{stem}.html", config_payload, sweep_rows
    )
    typer.echo(f"\nJSON:   {json_path}")
    typer.echo(f"report: {html_path}")
    if not sweep_rows:
        typer.secho(
            "No sweep results found; the robustness panel is empty. "
            "Run 'concordance match sweep' and re-run this to fill it in.",
            fg="yellow",
        )


@report_app.command("sweep")
def report_sweep(
    levels: Annotated[str | None, typer.Option("--levels", help="Comma-separated corruption levels.")] = None,
    strategies: Annotated[str | None, typer.Option("--strategies", help="Comma-separated strategy names.")] = None,
    providers: Annotated[int, typer.Option("--providers")] = 50_000,
    sanctions: Annotated[int, typer.Option("--sanctions")] = 5_000,
    workers: Annotated[int | None, typer.Option("--workers")] = None,
    reuse: Annotated[bool, typer.Option("--reuse/--regenerate")] = True,
    report: Annotated[Path | None, typer.Option("--report")] = None,
    seed: SeedOpt = None,
) -> None:
    """Alias for 'concordance match sweep' - the sweep is also a report."""
    _sweep(levels, strategies, providers, sanctions, workers, reuse, report, seed)


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Stage 6: runs, replay, diff, the worker
# --------------------------------------------------------------------------


@run_app.command("reconcile")
def run_reconcile(
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy", help="deterministic | fuzzy | probabilistic | probabilistic_llm"
        ),
    ] = "probabilistic",
    limit: Annotated[
        int | None, typer.Option("--limit", help="Score only the first N records.")
    ] = None,
    config_version: Annotated[
        str | None, typer.Option("--config", help="Scoring config version; default is the latest.")
    ] = None,
    chunk_size: Annotated[int, typer.Option("--chunk-size")] = 500,
    max_candidates: Annotated[int | None, typer.Option("--max-candidates")] = None,
    queue: Annotated[
        bool, typer.Option("--queue", help="Enqueue for a worker instead of running here.")
    ] = False,
    seed: SeedOpt = None,
) -> None:
    """Reconcile every sanction record against the provider master, and persist it."""
    from concordance.db.session import session_scope
    from concordance.jobs.reconcile import RunRequest, reconcile

    settings, _ = start(seed, echo_config=False)
    request = RunRequest(
        strategy=strategy,
        limit=limit,
        config_version=config_version,
        chunk_size=chunk_size,
        max_candidates=max_candidates,
        show_progress=True,
    )
    if queue:
        from concordance.jobs.worker import enqueue

        job_id = enqueue("reconcile", request.as_payload(), settings=settings)
        typer.secho(f"queued job {job_id} - start a worker with 'concordance jobs worker'", fg="green")
        return

    with session_scope(settings) as session:
        report = reconcile(session, settings, request)
    for key, value in report.as_dict().items():
        if key != "errors":
            typer.echo(f"{key:<24}{value}")
    for line in report.errors[:10]:
        typer.secho(f"  failed: {line}", fg="yellow")


@run_app.command("list")
def run_list(
    limit: Annotated[int, typer.Option("--limit")] = 10,
    seed: SeedOpt = None,
) -> None:
    """The most recent runs, newest first."""
    from concordance.db.repositories.matches import MatchRepository
    from concordance.db.session import session_scope

    settings, _ = start(seed, echo_config=False)
    with session_scope(settings) as session:
        page = MatchRepository(session).list_runs(limit=limit)
        for run in page.items:
            typer.echo(
                f"{run.id}  {run.created_at:%Y-%m-%d %H:%M}  {run.status:<10} {run.strategy:<18}"
                f" records={run.records_total:<6} match={run.matched_count:<6}"
                f" amb={run.ambiguous_count:<6} none={run.no_match_count:<6}"
                f" engine={run.engine_version}"
            )
        if not page.items:
            typer.secho("no runs yet - 'concordance run reconcile' makes one.", fg="yellow")


@run_app.command("replay")
def run_replay(
    run_id: Annotated[str, typer.Argument(help="The run to replay.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Replay even though the data no longer matches the snapshot."),
    ] = False,
    seed: SeedOpt = None,
) -> None:
    """Re-execute a finished run from its recorded provenance and report any drift."""
    import uuid as _uuid

    from concordance.db.session import session_scope
    from concordance.jobs.replay import SnapshotDriftError, replay

    settings, _ = start(seed, echo_config=False)
    with session_scope(settings) as session:
        try:
            report = replay(session, settings, _uuid.UUID(run_id), force=force, show_progress=True)
        except SnapshotDriftError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(code=2) from exc
    for line in report.lines():
        typer.echo(line)
    if not report.decision_identical:
        raise typer.Exit(code=1)


@run_app.command("diff")
def run_diff(
    run_a: Annotated[str, typer.Argument(help="The earlier run.")],
    run_b: Annotated[str, typer.Argument(help="The later run.")],
    confidence_delta: Annotated[
        float, typer.Option("--confidence-delta", help="Report confidence moves above this.")
    ] = 0.05,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    seed: SeedOpt = None,
) -> None:
    """What changed between two runs, and the config delta that explains it."""
    import json
    import uuid as _uuid

    from concordance.db.session import session_scope
    from concordance.jobs.diff import diff_runs

    settings, _ = start(seed, echo_config=False)
    with session_scope(settings) as session:
        report = diff_runs(
            session,
            _uuid.UUID(run_a),
            _uuid.UUID(run_b),
            confidence_delta=confidence_delta,
        )
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return
    for line in report.lines():
        typer.echo(line)


@jobs_app.command("worker")
def jobs_worker(
    kinds: Annotated[
        str | None, typer.Option("--kinds", help="Comma-separated job kinds; default is all.")
    ] = None,
    once: Annotated[bool, typer.Option("--once", help="Run a single job, then exit.")] = False,
    max_jobs: Annotated[int | None, typer.Option("--max-jobs")] = None,
    idle_timeout: Annotated[
        float | None,
        typer.Option("--idle-timeout", help="Exit after this many idle seconds."),
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Worker name in the lock column.")] = None,
    seed: SeedOpt = None,
) -> None:
    """Claim jobs from the queue and run them until signalled."""
    from concordance.jobs.worker import Worker, default_worker_name

    settings, _ = start(seed, echo_config=False)
    worker = Worker(
        settings=settings,
        name=name or default_worker_name(),
        kinds=[k.strip() for k in kinds.split(",")] if kinds else None,
        max_jobs=1 if once else max_jobs,
        idle_timeout=0.0 if once else idle_timeout,
    )
    worker.install_signal_handlers()
    stats = worker.run()
    typer.echo("  ".join(f"{k}={v}" for k, v in stats.as_dict().items()))


@jobs_app.command("enqueue")
def jobs_enqueue(
    kind: Annotated[str, typer.Argument(help="reconcile | eval | sweep | retune | expire_cases")],
    payload: Annotated[
        str | None, typer.Option("--payload", help="JSON object passed to the handler.")
    ] = None,
    seed: SeedOpt = None,
) -> None:
    """Put one job on the queue."""
    import json

    from concordance.jobs.registry import is_known, known_kinds
    from concordance.jobs.worker import enqueue

    settings, _ = start(seed, echo_config=False)
    import concordance.jobs.handlers  # noqa: F401  - registers the handlers

    if not is_known(kind):
        typer.secho(f"unknown kind {kind!r}. Known: {', '.join(known_kinds())}", fg="red")
        raise typer.Exit(code=2)
    job_id = enqueue(kind, json.loads(payload) if payload else {}, settings=settings)
    typer.secho(f"queued job {job_id} ({kind})", fg="green")


@jobs_app.command("list")
def jobs_list(
    status: Annotated[str | None, typer.Option("--status", help="PENDING|RUNNING|DONE|DEAD")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    seed: SeedOpt = None,
) -> None:
    """What is on the queue."""
    from concordance.db.repositories.jobs import JobRepository
    from concordance.db.session import session_scope

    settings, _ = start(seed, echo_config=False)
    with session_scope(settings) as session:
        page = JobRepository(session).list_jobs(status=status, limit=limit)
        for job in page.items:
            typer.echo(
                f"{job.id:<6} {job.kind:<14} {job.status:<8} attempts={job.attempts}/"
                f"{job.max_attempts} locked_by={job.locked_by or '-':<24} {job.last_error or ''}"[:160]
            )
        typer.echo(f"({page.total} total)")


@jobs_app.command("expire-cases")
def jobs_expire_cases(
    today: Annotated[
        str | None, typer.Option("--today", help="Pretend it is this date (YYYY-MM-DD).")
    ] = None,
    seed: SeedOpt = None,
) -> None:
    """Run the case-expiry transition now, in this process."""
    from datetime import date

    from concordance.cases.lifecycle import expire_cases
    from concordance.db.session import session_scope

    settings, _ = start(seed, echo_config=False)
    with session_scope(settings) as session:
        report = expire_cases(session, today=date.fromisoformat(today) if today else None)
    typer.echo(f"expired {report.expired} case(s) as of {report.today}")


def _dataset_for(settings: Settings, out: Path | None, corruption: float | None, seed: int) -> Path:
    """The dataset to work on, generating it first when a level was asked for."""
    if corruption is None:
        return out or settings.generated_dir
    from concordance.synth.pipeline import seed_dataset

    dataset = out or settings.DATA_DIR / f"corruption-{corruption:.1f}"
    seed_dataset(
        providers=50_000,
        sanctions=5_000,
        corruption=corruption,
        seed=seed,
        out_dir=dataset,
        write_excel=False,
    )
    return dataset


def _config_path(settings: Settings, config: Path | None, config_id: str | None) -> Path | None:
    """Resolve `--config` or `--config-id` to a file, or `None` to fit on the fly."""
    if config is not None:
        return config
    if config_id is None:
        return None
    name = config_id if config_id.endswith(".json") else f"{config_id}.json"
    path = settings.DATA_DIR / "configs" / name
    if not path.exists():
        available = sorted(p.stem for p in (settings.DATA_DIR / "configs").glob("config_*.json"))
        typer.secho(
            f"No config {config_id!r} under {settings.DATA_DIR / 'configs'}."
            + (f" Available: {', '.join(available)}" if available else " None fitted yet."),
            fg="red",
        )
        raise typer.Exit(code=1)
    return path


def _latest_config(settings: Settings) -> Path | None:
    configs = sorted((settings.DATA_DIR / "configs").glob("config_*.json"))
    return configs[-1] if configs else None


def _engine_and_config(
    settings: Settings, config: Path | None, prepared: Any, seed: int
) -> tuple[Any, dict[str, Any], str]:
    """Load a fitted config, or fit one now. Returns the engine, payload and id."""
    from concordance.eval.fitting import fit_config
    from concordance.matching.scoring_config import ScoringConfig

    path = config or _latest_config(settings)
    if path is not None:
        loaded = ScoringConfig.read(path)
        return loaded.engine(), loaded.as_dict(), loaded.config_id
    typer.secho("No fitted config on disk - fitting one now.", fg="yellow")
    report = fit_config(prepared, seed=seed, target_precision=settings.TARGET_PRECISION)
    return report.config.engine(), report.config.as_dict(), report.config.config_id


def _engine_for(settings: Settings, config: Path | None, prepared: Any, seed: int) -> Any:
    return _engine_and_config(settings, config, prepared, seed)[0]


def _adjudicator_for(settings: Settings, strategy: str) -> Any:
    """An adjudicator, but only for the strategy that has a grey band to hand over.

    Every other strategy gets `None`, which `build_strategy` ignores. Building
    one unconditionally would open HTTP clients for runs that never adjudicate.
    """
    from concordance.matching.strategies import StrategyName

    if StrategyName(strategy) is not StrategyName.PROBABILISTIC_LLM:
        return None
    from concordance.llm.ai_matcher import build_adjudicator

    adjudicator = build_adjudicator(settings)
    if getattr(adjudicator, "name", "") == "null":
        typer.secho(
            "probabilistic_llm requested but no adjudicator is configured - "
            "grey-band records will stay AMBIGUOUS and the run reports zero calls.",
            fg="yellow",
        )
    return adjudicator


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------


@api_app.command("serve")
def api_serve(
    host: Annotated[str, typer.Option(help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Restart on source changes.")] = False,
) -> None:
    """Run the API under uvicorn."""
    import uvicorn

    uvicorn.run("concordance.api.app:app", host=host, port=port, reload=reload)


@api_app.command("openapi")
def api_openapi(
    out: Annotated[Path, typer.Option(help="Where to write the schema.")] = Path("openapi.json"),
) -> None:
    """Write the OpenAPI schema the web client is generated from.

    Built from the app factory, not fetched from a running server: the contract
    is a property of the code, and regenerating the client should not need a
    database or a process listening on a port.
    """
    import json

    from concordance.api.app import create_app

    schema = create_app(get_settings()).openapi()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    typer.echo(f"wrote {out} ({len(schema.get('paths', {}))} paths)")


LevelsOpt = Annotated[
    str | None, typer.Option("--levels", help="Comma-separated corruption levels, e.g. 0.3,0.5.")
]
QueueOpt = Annotated[
    bool, typer.Option("--queue", help="Leave it for a worker instead of running it here.")
]


def _parse_levels(levels: str | None) -> list[float] | None:
    return [float(v) for v in levels.split(",") if v.strip()] if levels else None


def _lab(kind: str, queue: bool, request: Any) -> None:
    """Request an experiment as the system, then run it here unless queued.

    Run here, the row is written with no job at all, so a worker that happens to
    be running cannot claim the same experiment.
    """
    from concordance.audit.service import Actor
    from concordance.db.session import session_scope
    from concordance.errors import DomainError
    from concordance.lab import service

    settings, _ = start(None, echo_config=False)
    try:
        with session_scope(settings) as session:
            row = request(session, settings, Actor.system(), queue)
            lab_id = row.id
    except DomainError as exc:
        typer.secho(exc.message, fg="red")
        raise typer.Exit(code=1) from exc
    typer.echo(f"lab {kind} {lab_id} queued")
    if queue:
        typer.secho("start a worker with 'concordance jobs worker' to run it", fg="green")
        return
    run = service.run_sweep if kind == "sweep" else service.run_llm
    with session_scope(settings) as session:
        summary = run(session, settings, lab_id)
    for key, value in summary.items():
        typer.echo(f"{key:<12}{value}")


@lab_app.command("sweep")
def lab_sweep(
    levels: LevelsOpt = None,
    providers: Annotated[int | None, typer.Option("--providers")] = None,
    sanctions: Annotated[int | None, typer.Option("--sanctions")] = None,
    queue: QueueOpt = False,
    seed: SeedOpt = None,
) -> None:
    """Sweep every corruption level by every non-LLM strategy, for the Lab page."""
    from concordance.lab import service

    _lab(
        "sweep",
        queue,
        lambda session, settings, actor, enqueue: service.request_sweep(
            session,
            settings,
            actor,
            levels=_parse_levels(levels),
            providers=providers,
            sanctions=sanctions,
            seed=seed,
            enqueue=enqueue,
        ),
    )


@lab_app.command("llm")
def lab_llm(
    levels: LevelsOpt = None,
    sample: Annotated[
        int | None, typer.Option("--sample", help="Records per stratum; default 100.")
    ] = None,
    queue: QueueOpt = False,
) -> None:
    """Routed versus LLM-on-everything, sampled on the newest completed sweep."""
    from concordance.lab import service

    _lab(
        "llm",
        queue,
        lambda session, settings, actor, enqueue: service.request_llm(
            session, settings, actor, levels=_parse_levels(levels), sample=sample, enqueue=enqueue
        ),
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
