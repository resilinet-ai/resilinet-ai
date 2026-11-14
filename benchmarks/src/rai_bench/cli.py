"""``rai-bench``: run the arms, score a run, dry-run the whole chain (B-15).

Exit codes: 0 done, 2 the command could not run (bad inputs, broken seal,
evaluation fixtures without --allow-evaluation, results folder not empty).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from rai.validation.contract import ContractError
from rai_bench.arms import Arm, RaiArm
from rai_bench.runner import HarnessError, run
from rai_bench.scale import history_scale
from rai_bench.scoring import score
from sandboxbank.fixtures import build_fixtures

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

DRY_RUN_SEEDS = (15_001, 15_002)  # development range; never evaluation seeds
DEFAULT_CONTRACTS = Path("contracts") / "sandboxbank"


def _arms(names: list[str], contracts: Path) -> list[Arm]:
    arms: list[Arm] = []
    for name in names:
        if name != "R":
            raise HarnessError(f"arm {name!r} is not available yet (B1 and B2 freeze days 45-50)")
        try:
            arms.append(RaiArm(contracts))
        except (OSError, ContractError) as error:
            raise HarnessError(
                f"could not load the RAI contracts from {contracts}: {error}"
            ) from None
    return arms


def _fail(problem: Exception) -> typer.Exit:
    typer.echo(f"error: {problem}", err=True)
    return typer.Exit(2)


def _print_scores(scores: dict[str, Any]) -> None:
    typer.echo(
        f"{scores['instances']} seeded defect instances, {scores['clean_controls']} clean "
        f"controls; matching rule: {scores['matching_rule']}"
    )
    for arm, m in scores["arms"].items():
        recall, precision = m["recall"], m["precision_provisional"]
        alarms, latency, lead = (
            m["false_alarms_on_clean_controls"],
            m["latency_wall_seconds"],
            m["lead_time_seconds"],
        )
        typer.echo(
            f"arm {arm}: recall {recall['numerator']}/{recall['denominator']}, "
            f"provisional precision {precision['numerator']}/{precision['denominator']}, "
            f"false alarms {alarms['numerator']}/{alarms['denominator']} clean batches, "
            f"latency p50 {latency['p50']} s p95 {latency['p95']} s, lead time p50 {lead['p50']} s"
        )
        missed = [c for c, r in m["recall_by_class"].items() if r["numerator"] < r["denominator"]]
        typer.echo(f"  classes missed at least once: {', '.join(missed) or 'none'}")


@app.command("run")
def run_command(
    fixtures: Annotated[Path, typer.Argument(help="SandboxBank fixture set")],
    out: Annotated[Path, typer.Option(help="Results folder (must be new or empty)")],
    arm: Annotated[list[str] | None, typer.Option(help="Arm to run (repeatable)")] = None,
    contracts: Annotated[Path, typer.Option(help="RAI contracts folder")] = DEFAULT_CONTRACTS,
    allow_evaluation: Annotated[
        bool, typer.Option(help="Allow evaluation-seed fixtures (registered benchmark only)")
    ] = False,
) -> None:
    """Deliver every batch to every arm and record their alerts."""
    try:
        summary = run(
            fixtures, out, _arms(arm or ["R"], contracts), allow_evaluation=allow_evaluation
        )
    except HarnessError as problem:
        raise _fail(problem) from None
    counts = ", ".join(f"{name} {count}" for name, count in summary.alerts.items())
    typer.echo(f"{summary.batches} batches delivered; alerts: {counts}; results in {out}")


@app.command("score")
def score_command(
    fixtures: Annotated[Path, typer.Argument(help="The fixture set the run used")],
    run_dir: Annotated[Path, typer.Argument(help="Results folder of the run")],
) -> None:
    """Score a run against the sealed answer key (writes scores.json)."""
    try:
        scores = score(fixtures, run_dir)
    except HarnessError as problem:
        raise _fail(problem) from None
    _print_scores(scores)


@app.command("dry-run")
def dry_run_command(
    out: Annotated[
        Path | None,
        typer.Option(help="Results folder (default: benchmarks/results/dry-run-<UTC time>)"),
    ] = None,
    rows: Annotated[int, typer.Option(help="Rows per batch")] = 10_000,
    contracts: Annotated[Path, typer.Option(help="RAI contracts folder")] = DEFAULT_CONTRACTS,
) -> None:
    """Whole chain on a small development fixture set: one batch per defect class,
    three clean controls, run and score. No evaluation seeds are ever used."""
    if out is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = Path("benchmarks") / "results" / f"dry-run-{stamp}"
    try:
        if out.exists() and any(out.iterdir()):
            raise HarnessError(f"{out} is not empty; results are never overwritten")
        arms = _arms(["R"], contracts)
        data_seed, injector_seed = DRY_RUN_SEEDS
        build_fixtures(
            out / "fixtures",
            data_seed=data_seed,
            injector_seed=injector_seed,
            accounts=1_000,
            merchants=100,
            rows_per_batch=rows,
            batches_per_class=1,
            clean=3,
        )
        summary = run(out / "fixtures", out / "run", arms)
        typer.echo(f"{summary.batches} batches delivered to arm R; results in {out}")
        _print_scores(score(out / "fixtures", out / "run"))
    except HarnessError as problem:
        raise _fail(problem) from None


@app.command("history-scale")
def history_scale_command(
    out: Annotated[Path, typer.Option(help="Working folder (must be new or empty)")],
    keys: Annotated[int, typer.Option(help="Keys in the history")] = 18_000_000,
    repeats: Annotated[int, typer.Option(help="Validations to time")] = 5,
    contracts: Annotated[Path, typer.Option(help="RAI contracts folder")] = DEFAULT_CONTRACTS,
) -> None:
    """Time a 100k-row validation against a cross-batch history of protocol size."""
    try:
        result = history_scale(out, contracts, keys=keys, repeats=repeats)
    except HarnessError as problem:
        raise _fail(problem) from None
    typer.echo(json.dumps(result, indent=2, sort_keys=True))
