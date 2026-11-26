"""``rai-bench``: run the arms, score a run, dry-run the whole chain (B-15), profile B2 (B-21),
score RTO against the independent observer (B-44), and set up, play and score the chaos
benchmark (B-28).

Exit codes: 0 done, 1 ``chaos-seal`` found a plan that does not match its seal,
2 the command could not run (bad inputs, broken seal, evaluation fixtures without
--allow-evaluation, results folder not empty).
"""

from __future__ import annotations

import json
import os
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
from rai_bench.chaos_run import AlertLog, Api, Arms, load_setup, play, score_chaos, setup_rai
from rai_bench.h3 import h3
from rai_bench.live import B1Live, B2Monitor, b2_endpoints
from rai_bench.runner import HarnessError, delivered_table, run
from rai_bench.scale import history_scale
from rai_bench.scoring import score
from sandboxbank.dataset import write_dataset
from sandboxbank.fixtures import build_fixtures, write_clean_deliveries
from sandboxbank.generator import GeneratorConfig
from sandboxbank.schema import PIPELINE_TABLES

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

DRY_RUN_SEEDS = (15_001, 15_002)  # development range; never evaluation seeds
DEFAULT_CONTRACTS = Path("contracts") / "sandboxbank"
PROFILE_SEED = 45_000  # development range; the 30 clean batches B2 is profiled on
ARM_NAMES = ("B1", "B2", "R")


def _arms(names: list[str], contracts: Path, table: str = "transactions_raw") -> list[Arm]:
    arms: list[Arm] = []
    for name in names:
        if name not in ARM_NAMES:
            raise HarnessError(f"unknown arm {name!r}; choose from {', '.join(ARM_NAMES)}")
        if name == "B1":
            arms.append(B1Arm(table=table))
            continue
        if name == "B2":
            if not (B2_DIR / f"{table}.suite.json").is_file():
                raise HarnessError(f"arm B2 has no suite for {table}: run rai-bench b2-profile")
            try:
                arms.append(B2Arm(table=table))
            except ImportError:
                raise HarnessError("arm B2 needs Great Expectations: uv sync --extra b2") from None
            continue
        try:
            arms.append(RaiArm(contracts, table))
        except (OSError, ContractError) as error:
            raise HarnessError(
                f"could not load the RAI contracts from {contracts}: {error}"
            ) from None
    return arms


def _fixture_table(fixtures: Path) -> str:
    """The table a fixture set delivers; a missing manifest is left to the runner's check."""
    try:
        return delivered_table(json.loads((fixtures / "manifest.json").read_text("utf-8")))
    except (OSError, ValueError):
        return "transactions_raw"


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
        table = _fixture_table(fixtures)
        summary = run(
            fixtures, out, _arms(arm or ["R"], contracts, table), allow_evaluation=allow_evaluation
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
    pipeline: Annotated[str, typer.Option(help="P1, P2 or P3 (P3 needs the model extra)")] = "P1",
) -> None:
    """Whole chain on a small development fixture set: one batch per defect class,
    three clean controls, run and score. No evaluation seeds are ever used."""
    if out is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = Path("benchmarks") / "results" / f"dry-run-{stamp}"
    try:
        if out.exists() and any(out.iterdir()):
            raise HarnessError(f"{out} is not empty; results are never overwritten")
        table = PIPELINE_TABLES.get(pipeline)
        if table is None:
            raise HarnessError(f"unknown pipeline {pipeline!r}; choose P1, P2 or P3")
        arms = _arms(arm or ["R"], contracts, table)
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
            pipeline=pipeline,
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


def _secret(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise _fail(HarnessError(f"set {name} (a secret: never pass it on the command line)"))
    return value


@app.command("chaos-setup")
def chaos_setup_command(
    plan: Annotated[Path, typer.Argument(help="Sealed plan folder (its public registry.json)")],
    out: Annotated[Path, typer.Option(help="Where to write setup.json")],
    project: Annotated[str, typer.Option(help="RAI project id (a fresh one)")],
    environment_id: Annotated[str, typer.Option(help="SANDBOX-01's environment id")],
    api: Annotated[str, typer.Option(help="RAI API base URL")] = "http://127.0.0.1:8000",
) -> None:
    """Publish every registered scenario in RAI, have the reviewer approve it, and plan it
    with the post-recovery consistency checks. Needs RAI_BENCH_TENANT and two people's
    session tokens: RAI_BENCH_ENGINEER_TOKEN and RAI_BENCH_REVIEWER_TOKEN."""
    tenant = _secret("RAI_BENCH_TENANT")
    engineer = Api(api, _secret("RAI_BENCH_ENGINEER_TOKEN"), tenant=tenant)
    reviewer = Api(api, _secret("RAI_BENCH_REVIEWER_TOKEN"), tenant=tenant)
    try:
        setup = setup_rai(engineer, reviewer, plan, out, project=project,
                          environment_id=environment_id)  # fmt: skip
    except HarnessError as problem:
        raise _fail(problem) from None
    typer.echo(f"{len(setup['scenarios'])} scenarios published, approved and planned: {out}")


@app.command("chaos-play")
def chaos_play_command(
    plan: Annotated[Path, typer.Argument(help="Sealed plan folder")],
    setup: Annotated[Path, typer.Argument(help="setup.json from chaos-setup")],
    out: Annotated[Path, typer.Option(help="Run folder (resumed if it exists)")],
    expect: Annotated[str | None, typer.Option(help="Publicly committed fault-log SHA-256")] = None,
    allow_evaluation: Annotated[
        bool, typer.Option(help="Allow an evaluation plan (the registered benchmark only)")
    ] = False,
    api: Annotated[str, typer.Option(help="RAI API base URL")] = "http://127.0.0.1:8000",
    payments: Annotated[str, typer.Option(help="payments-api, as B1 and B2 see it")] = (
        "http://127.0.0.1:18080"
    ),
    etl: Annotated[str, typer.Option(help="etl-job health port")] = "http://127.0.0.1:18083",
    features: Annotated[str, typer.Option(help="feature-job health port")] = (
        "http://127.0.0.1:18084"
    ),
    baselines: Annotated[bool, typer.Option(help="Run B1 and B2 alongside")] = True,
) -> None:
    """Play the sealed plan through RAI, one injection at a time, with B1 and B2 watching.
    Needs RAI_BENCH_API_KEY (runs:start, runs:read) and, for B1, RAI_B1_ANALYTICS_DSN."""
    key = _secret("RAI_BENCH_API_KEY")
    arms = Arms()
    if baselines:
        dsn = _secret("RAI_B1_ANALYTICS_DSN")
        alerts = AlertLog(out / "alerts.jsonl")
        arms = Arms(
            B2Monitor(b2_endpoints(payments, etl, features), alerts),
            B1Live(out / "b1", {"RAI_B1_PAYMENTS": payments, "RAI_B1_ANALYTICS_DSN": dsn}, alerts),
        )
    try:
        played = play(Api(api, key), plan, load_setup(setup), out, expected=expect,
                      allow_evaluation=allow_evaluation, arms=arms)  # fmt: skip
    except HarnessError as problem:
        raise _fail(problem) from None
    typer.echo(f"{played} injections played; results in {out / 'injections.jsonl'}")


@app.command("chaos-score")
def chaos_score_command(
    run_dir: Annotated[Path, typer.Argument(help="Run folder from chaos-play")],
    observations: Annotated[Path, typer.Argument(help="The observer's log (sandboxbank observe)")],
    out: Annotated[Path | None, typer.Option(help="Write the full result here (JSON)")] = None,
) -> None:
    """Recovery attainment, H3's RTO half and the baselines' detection of each injection."""
    alerts = run_dir / "alerts.jsonl"
    try:
        result = score_chaos(run_dir / "injections.jsonl", observations,
                             alerts if alerts.is_file() else None)  # fmt: skip
    except HarnessError as problem:
        raise _fail(problem) from None
    overall = result["recovery"]["overall"]
    s = result["h3_rto"]["summary"]
    typer.echo(f"Recovery: RTO met {overall['rto']['met']}/{overall['runs']}, "
               f"RPO met {overall['rpo']['met']}/{overall['runs']} "
               f"({overall['measured']} measured)")  # fmt: skip
    typer.echo(f"H3 (RTO): {s['agreed']}/{s['scored']} within +/- {s['tolerance_seconds']:g} s; "
               f"{s['unobserved']} unobserved, {s['not_measured']} not measured; "
               f"{'supported' if s['supported'] else 'not supported'}")  # fmt: skip
    for arm, d in result.get("detection", {}).get("arms", {}).items():
        median = d["median_detection_seconds"]
        typer.echo(f"{arm}: detected {d['detected']}/{d['injections']}, median {median} s, "
                   f"{d['false_alarms']} false alarms")  # fmt: skip
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


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
    pipeline: Annotated[str, typer.Option(help="P1, P2 or P3 (P3 needs the model extra)")] = "P1",
) -> None:
    """Generate the clean development batches and write B2's profiled and tightened
    suites (Protocol section 3). Development seed only; never evaluation data."""
    try:
        if work.exists() and any(work.iterdir()):
            raise HarnessError(f"{work} is not empty")
        table = PIPELINE_TABLES.get(pipeline)
        if table is None:
            raise HarnessError(f"unknown pipeline {pipeline!r}; choose P1, P2 or P3")
        if pipeline == "P1":
            config = GeneratorConfig(seed=PROFILE_SEED, batches=batches, rows_per_batch=rows)
            write_dataset(config, work)
        else:  # the same seed and shape, delivered as the pipeline delivers it
            write_clean_deliveries(work, data_seed=PROFILE_SEED, pipeline=pipeline,
                                   batches=batches, rows_per_batch=rows)  # fmt: skip
        written = write_suites(work, out, table)
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
