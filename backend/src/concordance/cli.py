"""Command line entry point.

Command groups mirror the stages: ``data`` (Stage 1), ``match`` (Stages 2-3),
``llm`` (Stage 4), ``db`` (Stage 5), ``report`` (Stage 3 onward). Commands that
belong to a stage not yet built are registered anyway and exit with a message
naming the stage, so the surface of the finished system is visible from day one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

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
data_app = typer.Typer(help="Synthetic data generation and dataset inspection.", no_args_is_help=True)
match_app = typer.Typer(help="Normalization, blocking, scoring, calibration.", no_args_is_help=True)
llm_app = typer.Typer(help="LLM router, cache and grey-band adjudication.", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations and loaders.", no_args_is_help=True)
report_app = typer.Typer(help="Evaluation reports and sweeps.", no_args_is_help=True)

app.add_typer(data_app, name="data")
app.add_typer(match_app, name="match")
app.add_typer(llm_app, name="llm")
app.add_typer(db_app, name="db")
app.add_typer(report_app, name="report")

log = get_logger("cli")

SeedOpt = Annotated[int | None, typer.Option("--seed", "-s", help="Random seed; defaults to RANDOM_SEED.")]


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
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version and exit."),
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
    corruption: Annotated[float, typer.Option("--corruption", min=0.0, max=0.9, help="Corruption dial.")] = 0.5,
    seed: SeedOpt = None,
    out: Annotated[Path | None, typer.Option("--out", help="Output directory; defaults to DATA_DIR/generated.")] = None,
    excel: Annotated[bool, typer.Option("--excel/--no-excel", help="Also write the sanction Excel export.")] = True,
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
    scenario: Annotated[str | None, typer.Option("--scenario", help="Filter to one scenario tag.")] = None,
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
def match_normalize(seed: SeedOpt = None) -> None:
    """Normalize a dataset and persist the normalized columns."""
    _not_until(2, "Normalization")


@match_app.command("block")
def match_block(seed: SeedOpt = None) -> None:
    """Build the blocking index and report candidate-set recall."""
    _not_until(2, "Blocking")


@match_app.command("fit")
def match_fit(seed: SeedOpt = None) -> None:
    """Fit Fellegi-Sunter m/u parameters by EM."""
    _not_until(3, "The EM fit")


@match_app.command("run")
def match_run(seed: SeedOpt = None) -> None:
    """Score every sanction record against the provider master."""
    _not_until(3, "Scoring")


@match_app.command("calibrate")
def match_calibrate(seed: SeedOpt = None) -> None:
    """Fit isotonic calibration and choose thresholds for TARGET_PRECISION."""
    _not_until(3, "Calibration")


# --------------------------------------------------------------------------
# llm - Stage 4
# --------------------------------------------------------------------------


@llm_app.command("ping")
def llm_ping(seed: SeedOpt = None) -> None:
    """Check each configured provider in the fallback chain."""
    _not_until(4, "The LLM router")


@llm_app.command("adjudicate")
def llm_adjudicate(seed: SeedOpt = None) -> None:
    """Send grey-band pairs to the adjudicator."""
    _not_until(4, "Grey-band adjudication")


@llm_app.command("cache")
def llm_cache(seed: SeedOpt = None) -> None:
    """Show LLM response cache statistics."""
    _not_until(4, "The LLM cache")


# --------------------------------------------------------------------------
# db - Stage 5
# --------------------------------------------------------------------------


@db_app.command("upgrade")
def db_upgrade(seed: SeedOpt = None) -> None:
    """Run Alembic migrations."""
    _not_until(5, "Postgres persistence")


@db_app.command("load")
def db_load(seed: SeedOpt = None) -> None:
    """Load a generated Parquet dataset into Postgres."""
    _not_until(5, "The Postgres loader")


# --------------------------------------------------------------------------
# report - Stage 3 onward
# --------------------------------------------------------------------------


@report_app.command("eval")
def report_eval(seed: SeedOpt = None) -> None:
    """Evaluate a run against ground truth and write the HTML report."""
    _not_until(3, "Evaluation")


@report_app.command("sweep")
def report_sweep(seed: SeedOpt = None) -> None:
    """Sweep corruption levels and plot precision/recall against the dial."""
    _not_until(3, "The corruption sweep")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
