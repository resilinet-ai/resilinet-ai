"""``rai-bench``: run the arms, score a run, dry-run the whole chain (B-15), profile B2 (B-21),
score RTO against the independent observer (B-44).

Exit codes: 0 done, 1 ``chaos-seal`` found a plan that does not match its seal,
2 the command could not run (bad inputs, broken seal, evaluation fixtures without
--allow-evaluation, results folder not empty).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from rai.validation.contract import ContractError
from rai_bench.arms import Arm, RaiArm
from rai_bench.baselines import B2_DIR, B1Arm, B2Arm, effort, write_suites
from rai_bench.chaos import (
    DEFAULT_MAX_GAP_SECONDS,
    DEFAULT_MIN_GAP_SECONDS,
    build_plan,
    check_plan,
)
from rai_bench.h3 import h3
from rai_bench.runner import HarnessError, run
from rai_bench.scale import history_scale
from rai_bench.scoring import score
from sandboxbank.dataset import write_dataset
from sandboxbank.fixtures import build_fixtures
from sandboxbank.generator import GeneratorConfig

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

DRY_RUN_SEEDS = (15_001, 15_002)  # development range; never evaluation seeds
DEFAULT_CONTRACTS = Path("contracts") / "sandboxbank"
PROFILE_SEED = 45_000  # development range; the 30 clean batches B2 is profiled on
ARM_NAMES = ("B1", "B2", "R")


def _arms(names: list[str], contracts: Path) -> list[Arm]:
    arms: list[Arm] = []
    for name in names:
        if name not in ARM_NAMES:
            raise HarnessError(f"unknown arm {name!r}; choose from {', '.join(ARM_NAMES)}")
        if name == "B1":
            arms.append(B1Arm())
            continue
        if name == "B2":
            try:
                arms.append(B2Arm())
            except ImportError:
                raise HarnessError("arm B2 needs Great Expectations: uv sync --extra b2") from None
            continue
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
    arm: Annotated[list[str] | None, typer.Option(help="Arm to run (repeatable)")] = None,
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
        arms = _arms(arm or ["R"], contracts)
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
        names = ", ".join(a.name for a in arms)
        typer.echo(f"{summary.batches} batches delivered to arms {names}; results in {out}")
        _print_scores(score(out / "fixtures", out / "run"))
    except HarnessError as problem:
        raise _fail(problem) from None


@app.command("chaos-plan")
def chaos_plan_command(
    registry: Annotated[Path, typer.Argument(help="The pre-registered scenarios (YAML or JSON)")],
    out: Annotated[Path, typer.Option(help="Plan folder (must be new or empty)")],
    seed: Annotated[int, typer.Option(help="Plan seed: 0-99,999 development, 100,000+ evaluation")],
    min_gap: Annotated[int, typer.Option(help="Shortest gap between injections, s")] = (
        DEFAULT_MIN_GAP_SECONDS
    ),
    max_gap: Annotated[int, typer.Option(help="Longest gap between injections, s")] = (
        DEFAULT_MAX_GAP_SECONDS
    ),
) -> None:
    """Write the chaos benchmark's sealed fault plan (B-28): the planned fault log and its SEAL."""
    try:
        manifest = build_plan(
            registry, out, seed=seed, min_gap_seconds=min_gap, max_gap_seconds=max_gap
        )
    except HarnessError as problem:
        raise _fail(problem) from None
    typer.echo(
        f"{manifest['injections']} injections of {manifest['scenarios']} scenarios planned "
        f"in {out} ({manifest['seed_purpose']} seed)\n"
        f"Fault log SEAL: {manifest['fault_log_sha256']}\n"
        "Commit the SEAL publicly before any arm runs; keep answer_key/ away from the arms."
    )


@app.command("chaos-seal")
def chaos_seal_command(
    plan: Annotated[Path, typer.Argument(help="Plan folder")],
    expect: Annotated[
        str | None, typer.Option(help="Publicly committed SHA-256 to compare with")
    ] = None,
) -> None:
    """Check a fault plan against its SEAL, manifest and registry (exit 1 if it does not match)."""
    problems = check_plan(plan, expect)
    if problems:
        for problem in problems:
            typer.echo(f"{plan}: {problem}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{plan}: fault log matches its seal")


@app.command("h3")
def h3_command(
    measurements: Annotated[Path, typer.Argument(help="RAI's measurements (rai-bench-recovery/1)")],
    observations: Annotated[Path, typer.Argument(help="The observer's log (sandboxbank observe)")],
    out: Annotated[Path | None, typer.Option(help="Write the full result here (JSON)")] = None,
    hold: Annotated[
        float, typer.Option(help="Seconds healthy before restoration counts (stable_checks - 1)")
    ] = 2.0,
) -> None:
    """Protocol H3: RAI's RTO against the observer's ground truth, within +/- 2 s."""
    try:
        result = h3(measurements, observations, hold=hold)
    except HarnessError as problem:
        raise _fail(problem) from None
    s, ba = result["summary"], result["summary"]["bland_altman"]
    ci = "n/a" if s["ci95"] is None else f"{s['ci95'][0]:.4f}-{s['ci95'][1]:.4f}"
    typer.echo(
        f"H3: {s['agreed']}/{s['scored']} injections within +/- {s['tolerance_seconds']:g} s "
        f"(95% CI {ci}); {s['unobserved']} unobserved, excluded; "
        f"{'supported' if s['supported'] else 'not supported'}"
    )
    typer.echo(f"Bland-Altman: bias {ba['bias']} s, limits {ba['lower_limit']} to "
               f"{ba['upper_limit']} s over {ba['n']} injections")  # fmt: skip
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


@app.command("b2-profile")
def b2_profile_command(
    work: Annotated[Path, typer.Option(help="Folder for the clean batches (must be new or empty)")],
    out: Annotated[Path, typer.Option(help="Where to write the suites")] = B2_DIR,
    batches: Annotated[int, typer.Option(help="Clean development batches")] = 30,
    rows: Annotated[int, typer.Option(help="Rows per batch")] = 100_000,
) -> None:
    """Generate the clean development batches and write B2's profiled and tightened
    suites (Protocol section 3). Development seed only; never evaluation data."""
    try:
        if work.exists() and any(work.iterdir()):
            raise HarnessError(f"{work} is not empty")
        config = GeneratorConfig(seed=PROFILE_SEED, batches=batches, rows_per_batch=rows)
        write_dataset(config, work)
        written = write_suites(work, out)
    except HarnessError as problem:
        raise _fail(problem) from None
    for name, path in written.items():
        typer.echo(f"{name}: {path}")


@app.command("effort")
def effort_command(
    contracts: Annotated[Path, typer.Option(help="RAI contracts folder")] = DEFAULT_CONTRACTS,
) -> None:
    """Rule counts and lines of code per arm (Protocol section 3), as JSON."""
    typer.echo(json.dumps(effort(contracts), indent=2, sort_keys=True))


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
