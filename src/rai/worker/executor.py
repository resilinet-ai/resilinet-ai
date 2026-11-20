"""Execute one run from its pins (B-18: FR-06.1, FR-06.2).

queued -> preflight -> running -> evaluating -> passed | failed; preflight problems
end the run as ``refused`` with the reasons.

**Idempotent steps (FR-06.2).** Each plan item is one step. A step's observations
and its ``run_steps`` row are written in one transaction, so a worker killed
mid-step leaves nothing behind and the next worker simply does the step again;
a step that is recorded is never done again (the primary key would refuse it
anyway). The verdict is computed from what is recorded, so a resumed run reaches
the same verdict as an uninterrupted one.

**Data access.** A contract-rule step reads its asset only through a connector the
operator configured on this worker (``RAI_WORKER_CONNECTORS``), confined to a root
folder or a read-only database session. Tenants name a connector; they never
supply a path or a credential.

**Lineage (B-19: FR-04.1).** In the same transaction as its observations, a step
records every hop of the target's pinned upstream graph, the target into the run
and each reference dataset into the run, with the ``rai-frame-digest/1`` SHA-256
of each frame it read: the target, its references, and the upstream assets the
definition maps to a connector (``upstream``). Unmapped upstream assets appear as
hops without a digest. A step that could not read its data records no lineage.

**Chaos scenarios (B-26: FR-07.1, FR-07.2).** A scenario item runs its faults in
order through the signed chaos client (B-24). Preflight refuses the run unless this
worker has a signing key and the agent's URL, the scenario's latest review is still
an approval, and every fault passes the worker's checks against the environment's
allow-list and ceiling. Each fault is injected (its onset recorded), held until it
expires while the job's lease is renewed, then reverted through the agent (the revert
recorded). A refused or failed fault ends the step in ``error``; later faults are not
attempted. A worker that dies mid-scenario leaves the fault to the agent's expiry and
the watchdog (B-25); the retry finds each fault the agent already injected (409 with
its onset), waits out what remains, and records nothing twice.

**Cut short (aborted).** While it holds a fault, the worker asks the agent every few
seconds whether the fault is still open. A fault that ends before its declared time
because someone pulled the kill switch (``reverted_by: kill``) or the agent restarted
(``startup``) means the scenario did not run as reviewed, so nothing it measured can
be judged: the step is recorded in ``error`` with what happened, and the run ends
``aborted``, without a verdict, in the same transaction. Later faults and steps and
the recovery measurement are not attempted, and a retry finds the run finished.
Ordinary endings (the worker's own revert, the agent's expiry, the watchdog after the
declared time) are not aborts.

**Recovery (B-26b: FR-08.1, FR-08.2).** If the scenario has a recovery plan, the
worker reads the ledger head before the first fault (kept as the observation
``recovery.ledger_before``, so a retry measures from the same point), then after
the last revert probes every probe target until all of them answer
``stable_checks`` times in a row, or the window closes. RTO runs from the first
fault's onset to that validated restoration; RPO comes from the ledger head before
and after (:mod:`rai.recovery.measure`). Both are observations (``recovery.rto``,
``recovery.rpo``); a missed objective, no restoration, or lost ledger rows make the
step's verdict ``fail``.

**Consistency (FR-08.3).** A ``consistency_check`` step reconciles its target asset
against a declared upstream source, both read through worker connectors
(:mod:`rai.recovery.consistency`). It snapshots the source, then reads the target
and the source again every ``interval_seconds`` (renewing the lease) until nothing
is missing, doubled, unexpected or mismatched, or ``within_seconds`` has passed. The
four findings are observations; lineage records the hop with both frames' digests.
Placed after a scenario in a plan, it checks that the system recovered correctly.
With ``rpo`` (B-30), the source is SandboxBank's seeding log and the step also
records ``recovery.rpo_truth``: the run's first fault onset minus the earliest
acknowledged write the ledger lost, set beside the ledger-based ``recovery.rpo``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import polars as pl
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Connection, Engine, text

from rai.connectors import ConnectorError, connector
from rai.core.runs import TERMINAL, PinnedAsset, PinnedItem, Pins
from rai.core.scenarios import (
    FaultSpec,
    RecoveryPlan,
    ScenarioDefinition,
    latest_decision,
    scenario_ref,
)
from rai.core.tenancy import set_context
from rai.lineage.digest import frame_digest
from rai.privacy.canary import CanaryDefinition, Scan, needles, scan
from rai.recovery.consistency import (
    ConsistencyDefinition,
    GroundTruth,
    Reconciliation,
    ground_truth,
    prepare_source,
    prepare_target,
    reconcile,
)
from rai.recovery.measure import Head, Sample, rpo, rto
from rai.tevv.drift import ColumnDrift, DriftDefinition, drift
from rai.tevv.metrics import Evaluation, MetricDefinition, describe, evaluate
from rai.validation.contract import Contract
from rai.validation.engine import ValidationReport, validate
from rai.validation.history import SqliteKeyStore
from rai.validation.verdict import decide
from rai.worker import probes
from rai.worker.chaos import ChaosClient, FaultOutcome, fault_preflight, record_revert
from rai.worker.queue import Job

SUPPORTED_TYPES = (
    "contract_rule",
    "consistency_check",
    "drift_check",
    "tevv_metric",
    "privacy_canary",
)


class LeaseLost(Exception):
    """Another worker took the job over; stop without touching the run."""


class RunAborted(Exception):
    """A fault was cut short (kill switch, agent restart); the run is already
    recorded as ``aborted``, so stop without evaluating it."""


# Who ended a fault early: the run cannot be judged (see "Cut short" above).
CUT_SHORT = {
    "kill": "stopped by the kill switch",
    "startup": "reverted because the chaos agent restarted",
}


class ContractRuleDefinition(BaseModel):
    """What a ``contract_rule`` test case's ``definition`` holds."""

    model_config = ConfigDict(extra="forbid")
    connector: str
    environment: str
    references: dict[str, str] = {}
    fail_on: Literal["info", "warn", "critical"] = "critical"
    # Freshness is measured against the batch's newest row by default, exactly as
    # `rai validate` does without --as-of; "now" measures against the run's clock
    # (live feeds).
    freshness_reference: Literal["newest_row", "now"] = "newest_row"
    # Upstream assets to read and digest for lineage (B-19): asset name -> connector.
    # Unmapped upstream assets still appear as hops, without a digest.
    upstream: dict[str, str] = {}


@dataclass(frozen=True)
class WorkerConfig:
    name: str
    state_dir: Path
    connectors: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> config
    lease: timedelta = timedelta(minutes=5)
    chaos: ChaosClient | None = None  # B-24: signed fault requests; None = no faults here


@dataclass(frozen=True)
class Prepared:
    """A contract-rule step, checked in preflight."""

    item: PinnedItem
    definition: ContractRuleDefinition
    contract: Contract


@dataclass(frozen=True)
class PreparedConsistency:
    """A consistency-check step, checked in preflight (FR-08.3)."""

    item: PinnedItem
    definition: ConsistencyDefinition
    source: PinnedAsset  # the upstream asset, from the pinned lineage


@dataclass(frozen=True)
class PreparedDrift:
    """A drift-check step, checked in preflight (FR-09.2)."""

    item: PinnedItem
    definition: DriftDefinition
    reference: PinnedAsset  # pinned when the run was queued


@dataclass(frozen=True)
class PreparedMetric:
    """A model-evaluation step, checked in preflight (FR-09.1)."""

    item: PinnedItem
    definition: MetricDefinition


@dataclass(frozen=True)
class PreparedCanary:
    """A canary privacy check, checked in preflight (FR-05.1)."""

    item: PinnedItem
    definition: CanaryDefinition
    key: PinnedAsset  # the answer key, pinned when the run was queued


@dataclass(frozen=True)
class PreparedScenario:
    """A scenario step, checked in preflight: one signed fault per spec, in order."""

    item: PinnedItem
    specs: tuple[FaultSpec, ...]
    recovery: RecoveryPlan | None = None
    probe_urls: tuple[str, ...] = ()  # from the allow-list, for the plan's probes
    ledger_url: str | None = None


@contextmanager
def tenant_transaction(engine: Engine, job: Job, worker: str) -> Iterator[Connection]:
    with engine.begin() as conn:
        set_context(conn, job.tenant_id, "worker", worker)
        yield conn


def move(
    engine: Engine,
    job: Job,
    worker: str,
    state: str,
    *,
    verdict: str | None = None,
    reason: str | None = None,
) -> None:
    """One state transition, in its own transaction (the trigger enforces the machine)."""
    with tenant_transaction(engine, job, worker) as conn:
        conn.execute(
            text(
                "UPDATE rai.runs SET state = CAST(:s AS rai.run_state), "
                "verdict = CAST(:v AS rai.run_verdict), reason = COALESCE(:r, reason) "
                "WHERE run_id = :run"
            ),
            {"s": state, "v": verdict, "r": reason, "run": job.run_id},
        )


def _load_contract(conn: Connection, item: PinnedItem) -> Contract:
    if item.contract is None:
        raise ValueError("no contract is pinned")
    row = conn.execute(
        text("SELECT spec, spec_sha256 FROM rai.data_contracts WHERE contract_id = :c"),
        {"c": item.contract.contract_id},
    ).first()
    if row is None:
        raise ValueError(f"contract {item.contract.ref} is not available")
    contract = Contract.model_validate(row.spec)
    if not (contract.spec_sha256 == row.spec_sha256 == item.contract.spec_sha256):
        raise ValueError(f"contract {item.contract.ref} does not match its pinned SHA-256")
    return contract


def _environment_problems(conn: Connection, pins: Pins, name: str) -> list[str]:
    """Deny by default (B-22, NFR-SAF-1): a test runs only in a registered, active
    SANDBOX or STAGING environment of the tenant, and only in the run's environment."""
    status = conn.execute(
        text("SELECT status::text FROM rai.environments WHERE name = :n"), {"n": name}
    ).scalar_one_or_none()
    if status is None:
        return [f"environment {name!r} is not registered"]
    if status != "active":
        return [f"environment {name!r} is retired"]
    if pins.environment is not None and pins.environment.name != name:
        return [f"environment {name!r} is not the run's environment {pins.environment.name!r}"]
    return []


def _connector_problems(config: WorkerConfig, name: str, environment: str) -> list[str]:
    source = config.connectors.get(name)
    if source is None:
        return [f"connector {name!r} is not on this worker"]
    if source.get("environment") != environment:
        return [f"connector {name!r} is not in environment {environment!r}"]
    return []


def _scenario_problems(
    conn: Connection, pins: Pins, config: WorkerConfig, item: PinnedItem, job: Job
) -> tuple[PreparedScenario | None, list[str]]:
    """The worker's preflight for a chaos scenario (FR-07.2, NFR-SAF-1)."""
    scenario = item.scenario
    assert scenario is not None  # noqa: S101 - the caller dispatches on it
    if config.chaos is None:
        return None, ["this worker is not configured for faults (RAI_WORKER_SIGNING_KEY)"]
    decision = latest_decision(conn, scenario.scenario_id)
    refusal = "the scenario's approval was withdrawn" if decision is None else decision.refusal()
    if refusal is not None:  # withdrawn, or expired (B-43), after the run was queued
        return None, [f"{refusal} after the run was queued"]
    definition = ScenarioDefinition(blast_radius=scenario.blast_radius, faults=scenario.faults)
    specs = tuple(definition.specs(scenario_ref(scenario.scenario_id, scenario.version)))
    problems: list[str] = []
    for n, spec in enumerate(specs, start=1):
        _, found = fault_preflight(
            conn, pins, spec, run_id=job.run_id, tenant_id=job.tenant_id,
            step_key=fault_key(item, n),
        )  # fmt: skip
        problems.extend(f"fault {n} ({spec.type} on {spec.target}): {p}" for p in found)
    plan = scenario.recovery
    if plan is None:
        return (None, problems) if problems else (PreparedScenario(item, specs), [])
    urls = _health_urls(conn, pins, [*plan.probes, *([plan.ledger] if plan.ledger else [])])
    for name in sorted({*plan.probes, *([plan.ledger] if plan.ledger else [])} - set(urls)):
        problems.append(f"recovery: {name!r} is not on the allow-list with a health_url")
    if problems:
        return None, problems
    return PreparedScenario(
        item, specs, plan, tuple(urls[name] for name in plan.probes),
        urls[plan.ledger] if plan.ledger else None,
    ), []  # fmt: skip


def _consistency_problems(
    conn: Connection, pins: Pins, config: WorkerConfig, item: PinnedItem
) -> tuple[PreparedConsistency | None, list[str]]:
    """The worker's preflight for a consistency check (FR-08.3), with a contract rule's
    rules: a registered environment, connectors on this worker, and a source declared
    upstream of the target (tenants name assets, never locations)."""
    assert item.test is not None  # noqa: S101 - the caller dispatches on it
    try:
        definition = ConsistencyDefinition.model_validate(item.test.definition)
    except ValidationError as error:
        fields = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'definition'}: {e['msg']}"
            for e in error.errors()
        )
        return None, [f"the consistency check's definition is not valid ({fields})"]
    problems = _environment_problems(conn, pins, definition.environment)
    for name in dict.fromkeys((definition.connector, definition.source.connector)):
        problems += _connector_problems(config, name, definition.environment)
    if item.asset is None or not item.asset.locator:
        problems.append("the target asset has no locator")
    sources = [hop.source for hop in item.upstream if hop.source.name == definition.source.asset]
    target = item.asset.name if item.asset else "the target"
    if not sources:
        problems.append(f"{definition.source.asset!r} is not upstream of {target!r} in the "
                        "pinned lineage; declare it as an input first")  # fmt: skip
    elif not sources[0].locator:
        problems.append(f"upstream asset {definition.source.asset!r} has no locator")
    if problems:
        return None, problems
    return PreparedConsistency(item, definition, sources[0]), []


def _drift_problems(
    conn: Connection, pins: Pins, config: WorkerConfig, item: PinnedItem
) -> tuple[PreparedDrift | None, list[str]]:
    """The worker's preflight for a drift check (FR-09.2): a valid definition, a
    registered environment, connectors on this worker, and both assets locatable."""
    assert item.test is not None  # noqa: S101 - the caller dispatches on it
    try:
        definition = DriftDefinition.model_validate(item.test.definition)
    except ValidationError as error:
        fields = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'definition'}: {e['msg']}"
            for e in error.errors()
        )
        return None, [f"the drift check's definition is not valid ({fields})"]
    problems = _environment_problems(conn, pins, definition.environment)
    for name in dict.fromkeys((definition.connector, definition.reference.connector)):
        problems += _connector_problems(config, name, definition.environment)
    if item.asset is None or not item.asset.locator:
        problems.append("the target asset has no locator")
    reference = item.reference
    if reference is None:
        problems.append(f"the reference asset {definition.reference.asset!r} was not pinned "
                        "with the run")  # fmt: skip
    elif not reference.locator:
        problems.append(f"the reference asset {reference.name!r} has no locator")
    if problems or reference is None:
        return None, problems
    return PreparedDrift(item, definition, reference), []


def _metric_problems(
    conn: Connection, pins: Pins, config: WorkerConfig, item: PinnedItem
) -> tuple[PreparedMetric | None, list[str]]:
    """The worker's preflight for a model evaluation (FR-09.1): a valid definition, a
    registered environment, its connector on this worker, and a locatable asset."""
    assert item.test is not None  # noqa: S101 - the caller dispatches on it
    try:
        definition = MetricDefinition.model_validate(item.test.definition)
    except ValidationError as error:
        fields = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'definition'}: {e['msg']}"
            for e in error.errors()
        )
        return None, [f"the model evaluation's definition is not valid ({fields})"]
    problems = _environment_problems(conn, pins, definition.environment)
    problems += _connector_problems(config, definition.connector, definition.environment)
    if item.asset is None or not item.asset.locator:
        problems.append("the target asset has no locator")
    if problems:
        return None, problems
    return PreparedMetric(item, definition), []


def _canary_problems(
    conn: Connection, pins: Pins, config: WorkerConfig, item: PinnedItem
) -> tuple[PreparedCanary | None, list[str]]:
    """The worker's preflight for a canary check (FR-05.1): a valid definition, a
    registered environment, connectors on this worker, and both assets locatable."""
    assert item.test is not None  # noqa: S101 - the caller dispatches on it
    try:
        definition = CanaryDefinition.model_validate(item.test.definition)
    except ValidationError as error:
        fields = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'definition'}: {e['msg']}"
            for e in error.errors()
        )
        return None, [f"the canary check's definition is not valid ({fields})"]
    problems = _environment_problems(conn, pins, definition.environment)
    for name in dict.fromkeys((definition.connector, definition.key.connector)):
        problems += _connector_problems(config, name, definition.environment)
    if item.asset is None or not item.asset.locator:
        problems.append("the target asset has no locator")
    key = item.reference
    if key is None:
        problems.append(f"the canary key {definition.key.asset!r} was not pinned with the run")
    elif not key.locator:
        problems.append(f"the canary key {key.name!r} has no locator")
    if problems or key is None:
        return None, problems
    return PreparedCanary(item, definition, key), []


def _health_urls(conn: Connection, pins: Pins, names: list[str]) -> dict[str, str]:
    """Allow-listed targets of the run's environment that have a health_url."""
    if pins.environment is None:
        return {}
    rows = conn.execute(
        text(
            "SELECT name, health_url FROM rai.environment_targets "
            "WHERE environment_id = :e AND name = ANY(:n) AND health_url IS NOT NULL"
        ),
        {"e": pins.environment.environment_id, "n": names},
    ).all()
    return {row.name: row.health_url for row in rows}


def fault_key(item: PinnedItem, n: int) -> str:
    """The step key of a scenario's nth fault: what the agent's replay guard keys on."""
    return f"{item.step_key}.fault-{n}"


def preflight(
    conn: Connection, pins: Pins, config: WorkerConfig, job: Job
) -> tuple[dict[str, Step], list[str]]:
    """Check every item can run here; returns the prepared steps and the problems."""
    prepared: dict[str, Step] = {}
    problems: list[str] = []
    for item in pins.items:
        where = f"item {item.position} ({item.label})"
        if item.scenario is not None:
            scenario, found = _scenario_problems(conn, pins, config, item, job)
            problems.extend(f"{where}: {problem}" for problem in found)
            if scenario is not None:
                prepared[item.step_key] = scenario
            continue
        assert item.test is not None  # noqa: S101 - one of test and scenario
        if item.test.type not in SUPPORTED_TYPES:
            problems.append(f"{where}: test type {item.test.type} is not supported yet")
            continue
        if item.test.type == "consistency_check":
            check, found = _consistency_problems(conn, pins, config, item)
            problems.extend(f"{where}: {problem}" for problem in found)
            if check is not None:
                prepared[item.step_key] = check
            continue
        if item.test.type == "drift_check":
            compare, found = _drift_problems(conn, pins, config, item)
            problems.extend(f"{where}: {problem}" for problem in found)
            if compare is not None:
                prepared[item.step_key] = compare
            continue
        if item.test.type == "privacy_canary":
            sentinel, found = _canary_problems(conn, pins, config, item)
            problems.extend(f"{where}: {problem}" for problem in found)
            if sentinel is not None:
                prepared[item.step_key] = sentinel
            continue
        if item.test.type == "tevv_metric":
            metric, found = _metric_problems(conn, pins, config, item)
            problems.extend(f"{where}: {problem}" for problem in found)
            if metric is not None:
                prepared[item.step_key] = metric
            continue
        try:
            definition = ContractRuleDefinition.model_validate(item.test.definition)
        except ValidationError as error:
            fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in error.errors())
            problems.append(f"{where}: the definition needs connector and environment ({fields})")
            continue
        unusable = [
            f"{where}: {problem}"
            for problem in _environment_problems(conn, pins, definition.environment)
            + _connector_problems(config, definition.connector, definition.environment)
        ]
        if unusable:
            problems.extend(unusable)
            continue
        if item.asset is None or not item.asset.locator:
            problems.append(f"{where}: the target asset has no locator")
            continue
        upstream = {hop.source.name: hop.source for hop in item.upstream}
        lineage_problems = []
        for name, connector_name in sorted(definition.upstream.items()):
            if name not in upstream:
                lineage_problems.append(
                    f"{where}: {name!r} is not upstream of {item.asset.name!r} in the pinned "
                    "lineage; declare it as an input first"
                )
            elif not upstream[name].locator:
                lineage_problems.append(f"{where}: upstream asset {name!r} has no locator")
            else:
                lineage_problems.extend(
                    f"{where}: upstream {name!r}: {problem}"
                    for problem in _connector_problems(
                        config, connector_name, definition.environment
                    )
                )
        if lineage_problems:
            problems.extend(lineage_problems)
            continue
        try:
            contract = _load_contract(conn, item)
        except ValueError as error:
            problems.append(f"{where}: {error}")
            continue
        prepared[item.step_key] = Prepared(item, definition, contract)
    return prepared, problems


def _observations(report: ValidationReport, step: Prepared, fail_on: str) -> list[dict[str, Any]]:
    rows = []
    for result in report.results:
        observed: dict[str, Any] = {
            "message": result.message,
            "sample_rows": list(result.sample_rows),
        }
        if result.kind == "schema":
            observed["changes"] = [c.describe() for c in report.schema_changes]
        expected = {
            "severity": result.severity, "threshold": result.threshold,
            "columns": list(result.columns), "fail_on": fail_on,
            "contract": step.item.contract.ref if step.item.contract else None,
        }  # fmt: skip
        rows.append(
            {
                "kind": "rule_result",
                "rule": result.rule_id,
                "outcome": result.outcome,
                "rows": result.row_count,
                "failing": result.failing_count,
                "observed": json.dumps(observed),
                "expected": json.dumps(expected),
            }
        )
    return rows


@dataclass(frozen=True)
class Digested:
    sha256: str
    rows: int


def _digested(frame: pl.DataFrame) -> Digested:
    return Digested(frame_digest(frame), frame.height)


def _edges(
    item: PinnedItem, read: dict[UUID, Digested], references: dict[str, Digested]
) -> list[dict[str, Any]]:
    """Every pinned hop upstream of the target, the target into the run, and each
    reference dataset into the run; digests where this step read the data."""
    target = item.asset
    assert target is not None  # noqa: S101 - checked in preflight

    def edge(
        from_asset: UUID | None, from_name: str, seen: Digested | None,
        to_asset: UUID | None, transform: str | None,
    ) -> dict[str, Any]:  # fmt: skip
        to_seen = read.get(to_asset) if to_asset is not None else None
        return {
            "from_asset": from_asset, "from_name": from_name,
            "from_digest": seen.sha256 if seen else None, "from_rows": seen.rows if seen else None,
            "to_asset": to_asset, "to_digest": to_seen.sha256 if to_seen else None,
            "transform": transform,
        }  # fmt: skip

    edges = [
        edge(hop.source.asset_id, hop.source.name, read.get(hop.source.asset_id), hop.into,
             hop.transform_ref)
        for hop in item.upstream
    ]  # fmt: skip
    edges.append(edge(target.asset_id, target.name, read[target.asset_id], None, None))
    edges.extend(
        edge(None, f"reference:{name}", seen, None, None)
        for name, seen in sorted(references.items())
    )
    return edges


class Executor:
    def __init__(
        self,
        engine: Engine,
        config: WorkerConfig,
        *,
        renew: Callable[[Job], bool],
        before_commit: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        probe_interval: float = 1.0,
        hold_check: float = 5.0,
    ) -> None:
        self.engine, self.config, self.renew = engine, config, renew
        self.before_commit = before_commit  # test hook: called inside a step's transaction
        self.sleep = sleep  # test hook: holding a fault for its duration
        self.probe_interval = probe_interval  # seconds between recovery probe rounds
        self.hold_check = hold_check  # seconds between "is the fault still open?" checks

    def execute(self, job: Job) -> None:
        name = self.config.name
        with tenant_transaction(self.engine, job, name) as conn:
            row = conn.execute(
                text("SELECT state::text, pins FROM rai.runs WHERE run_id = :r"), {"r": job.run_id}
            ).one()
        if row.state in TERMINAL:
            return
        pins = Pins.model_validate(row.pins)
        if row.state == "queued":
            move(self.engine, job, name, "preflight")
        with tenant_transaction(self.engine, job, name) as conn:
            prepared, problems = preflight(conn, pins, self.config, job)
        if row.state in ("queued", "preflight"):
            if problems:
                move(self.engine, job, name, "refused", reason="; ".join(problems)[:2000])
                return
            move(self.engine, job, name, "running")
        if row.state in ("queued", "preflight", "running"):
            try:
                self._steps(job, pins, prepared)
            except RunAborted:
                return  # recorded with the step; there is nothing to evaluate
            move(self.engine, job, name, "evaluating")
        self._decide(job, pins)

    def _steps(self, job: Job, pins: Pins, prepared: dict[str, Step]) -> None:
        with tenant_transaction(self.engine, job, self.config.name) as conn:
            done = {
                r.step_key
                for r in conn.execute(
                    text("SELECT step_key FROM rai.run_steps WHERE run_id = :r"), {"r": job.run_id}
                )
            }
        for item in pins.items:
            if item.step_key in done:
                continue  # recorded by an earlier attempt: never repeated
            if not self.renew(job):
                raise LeaseLost(job.job_id)
            step = prepared.get(item.step_key)
            if step is None:  # preflight passed before, but not now: the worker changed
                raise RuntimeError(f"{item.step_key} can no longer run on this worker")
            if isinstance(step, PreparedScenario):
                self._scenario(job, pins, step)
            elif isinstance(step, PreparedConsistency):
                self._consistency(job, step)
            elif isinstance(step, PreparedDrift):
                self._drift(job, step)
            elif isinstance(step, PreparedMetric):
                self._metric(job, step)
            elif isinstance(step, PreparedCanary):
                self._canary(job, step)
            else:
                self._step(job, step)

    # ---- chaos scenarios (B-26) ---------------------------------------------------------------

    def _scenario(self, job: Job, pins: Pins, step: PreparedScenario) -> None:
        started = datetime.now(UTC)
        chaos = self.config.chaos
        assert chaos is not None  # noqa: S101 - checked in preflight
        failure: str | None = None
        before: Head | None = None
        if step.ledger_url is not None:
            try:
                before = self._ledger_before(job, step)
            except ValueError as error:
                failure = f"recovery: could not read the ledger head before the faults: {error}"
        onset: datetime | None = None
        reverted: datetime | None = None
        aborted: str | None = None
        for n, spec in enumerate(step.specs, start=1):
            if failure or aborted:
                break
            if not self.renew(job):
                raise LeaseLost(job.job_id)
            key = fault_key(step.item, n)
            with tenant_transaction(self.engine, job, self.config.name) as conn:
                outcome = chaos.inject(
                    conn, pins, spec, run_id=job.run_id, tenant_id=job.tenant_id, step_key=key
                )
            if outcome.status != "injected" or outcome.fault is None:
                reasons = "; ".join(outcome.reasons) or outcome.status
                failure = (f"fault {n} ({spec.type} on {spec.target}) {outcome.status} "
                           f"by the {outcome.by}: {reasons}")  # fmt: skip
                break
            onset = onset or _moment(outcome.fault.get("onset"))
            self._hold(job, outcome)
            status, body = chaos.agent.revert(str(outcome.fault["fault_id"]))
            with tenant_transaction(self.engine, job, self.config.name) as conn:
                record_revert(conn, job.run_id, key, spec, status, body)
            if not (status == 200 and isinstance(body, dict) and body.get("status") == "reverted"):
                failure = f"fault {n} ({spec.type} on {spec.target}) was not reverted: {body}"
                break
            reverted = _moment(body.get("reverted_at")) or datetime.now(UTC)
            why = CUT_SHORT.get(str(body.get("reverted_by")))
            if why is not None:
                aborted = (
                    f"aborted: fault {n} ({spec.type} on {spec.target}) was {why} at "
                    f"{_stamp(reverted)}, before its declared end; the scenario did not run "
                    "as reviewed, so its recovery was not measured and the run has no verdict"
                )
        missed: list[str] = []
        measurement: dict[str, Any] | None = None
        if failure is None and aborted is None and step.recovery is not None and onset and reverted:
            missed, measurement = self._measure(job, step, onset, reverted, before)
        problem = failure or aborted
        with tenant_transaction(self.engine, job, self.config.name) as conn:
            self._record_step(
                conn, job, step.item, started,
                state="error" if problem else "done",
                verdict=None if problem else ("fail" if missed else "pass"),
                error=problem or ("; ".join(missed) if missed else None),
            )  # fmt: skip
            if measurement is not None:  # same transaction as the step (B-29)
                _record_measurement(conn, job, step, measurement)
            if aborted is not None:  # with the step, so a retry finds the run finished
                conn.execute(
                    text("UPDATE rai.runs SET state = 'aborted', reason = :r WHERE run_id = :run"),
                    {"r": aborted[:2000], "run": job.run_id},
                )
            if self.before_commit is not None:
                self.before_commit(step.item.step_key)
        if aborted is not None:
            raise RunAborted(aborted)

    def _ledger_before(self, job: Job, step: PreparedScenario) -> Head:
        """The ledger head before the first fault, read once: a retry reads it back from
        the observation, so RPO is measured from the same point."""
        key = step.item.step_key
        with tenant_transaction(self.engine, job, self.config.name) as conn:
            kept = conn.execute(
                text("SELECT observed_value FROM rai.observations WHERE run_id = :r "
                     "AND step_key = :k AND rule = 'recovery.ledger_before'"),
                {"r": job.run_id, "k": key},
            ).scalar_one_or_none()  # fmt: skip
        if kept is not None:
            return Head.parse(kept)
        assert step.ledger_url is not None  # noqa: S101 - the caller checks
        status, body = probes.get(step.ledger_url)
        if status != 200:
            raise ValueError(f"HTTP {status or 'no answer'} from the ledger head")
        head = Head.parse(body)
        with tenant_transaction(self.engine, job, self.config.name) as conn:
            self._observe(conn, job, key, "recovery.ledger_before", "pass", _head_dict(head), {})
        return head

    def _measure(
        self, job: Job, step: PreparedScenario, onset: datetime, reverted: datetime,
        before: Head | None,
    ) -> tuple[list[str], dict[str, Any]]:  # fmt: skip
        """Probe until restored (or the window closes), then RTO and RPO as observations.
        Returns the objectives missed, and the row for ``recovery_measurements``."""
        plan = step.recovery
        assert plan is not None  # noqa: S101 - the caller checks
        samples: list[Sample] = []
        closes = datetime.now(UTC) + timedelta(seconds=plan.window_seconds)
        measured = rto(onset, reverted, samples, plan.stable_checks)
        while measured.restored_at is None and datetime.now(UTC) < closes:
            if not self.renew(job):
                raise LeaseLost(job.job_id)
            at = datetime.now(UTC)
            samples.append(Sample(at, all(probes.healthy(url) for url in step.probe_urls)))
            measured = rto(onset, reverted, samples, plan.stable_checks)
            if measured.restored_at is None:
                self.sleep(self.probe_interval)
        seconds = measured.seconds
        rto_missed: list[str] = []
        if seconds is None:
            rto_missed.append(f"not restored within {plan.window_seconds} s of the last revert")
        elif plan.rto_seconds is not None and seconds > plan.rto_seconds:
            rto_missed.append(f"RTO {seconds} s exceeds the objective of {plan.rto_seconds} s")
        rto_observed = {
            "seconds": seconds, "onset": _stamp(onset), "reverted_at": _stamp(reverted),
            "restored_at": _stamp(measured.restored_at), "samples": len(samples),
            "probes": list(plan.probes),
        }  # fmt: skip
        rto_expected = {"rto_seconds": plan.rto_seconds, "stable_checks": plan.stable_checks,
                        "window_seconds": plan.window_seconds}  # fmt: skip
        rpo_missed: list[str] = []
        rpo_observed: dict[str, Any] | None = None
        if before is not None and step.ledger_url is not None:
            rpo_observed, rpo_missed = self._rpo(step.ledger_url, onset, before, plan)
        key = step.item.step_key
        with tenant_transaction(self.engine, job, self.config.name) as conn:
            self._observe(conn, job, key, "recovery.rto", "fail" if rto_missed else "pass",
                          rto_observed, rto_expected)  # fmt: skip
            if rpo_observed is not None:
                self._observe(conn, job, key, "recovery.rpo", "fail" if rpo_missed else "pass",
                              rpo_observed, {"rpo_seconds": plan.rpo_seconds})  # fmt: skip
        measurement = {
            "onset_at": onset, "reverted_at": reverted, "restored_at": measured.restored_at,
            "rto_seconds": seconds, "rto_objective_seconds": plan.rto_seconds,
            "rto_met": None if plan.rto_seconds is None else not rto_missed,
            "rpo_attempted": rpo_observed is not None,
            "rpo_seconds": None if rpo_observed is None else rpo_observed.get("seconds"),
            "rpo_objective_seconds": plan.rpo_seconds,
            "rpo_met": None if plan.rpo_seconds is None else not rpo_missed,
            "lost_rows": None if rpo_observed is None else rpo_observed.get("lost_rows"),
            "stable_checks": plan.stable_checks, "window_seconds": plan.window_seconds,
        }  # fmt: skip
        return rto_missed + rpo_missed, measurement

    @staticmethod
    def _rpo(
        url: str, onset: datetime, before: Head, plan: RecoveryPlan
    ) -> tuple[dict[str, Any], list[str]]:
        """What the ledger head shows after recovery, and the RPO objectives missed."""
        status, body = probes.get(url)
        try:
            if status != 200:
                raise ValueError(f"HTTP {status or 'no answer'} from the ledger head")
            after = Head.parse(body)
        except ValueError as error:
            unread = {"seconds": None, "error": str(error), "before": _head_dict(before)}
            return unread, [f"RPO: could not read the ledger head after recovery: {error}"]
        result = rpo(onset, before, after)
        missed = []
        if result.seconds is None:
            missed.append(f"RPO unknown: {result.lost_rows} ledger rows lost")
        else:
            if result.lost_rows:
                missed.append(f"{result.lost_rows} committed ledger rows were lost")
            if plan.rpo_seconds is not None and result.seconds > plan.rpo_seconds:
                missed.append(
                    f"RPO {result.seconds} s exceeds the objective of {plan.rpo_seconds} s"
                )
        observed: dict[str, Any] = {
            "seconds": result.seconds, "lost_rows": result.lost_rows,
            "before": _head_json(before), "after": _head_json(after),
            "recovery_point": _head_json(result.recovery_point),
        }  # fmt: skip
        return observed, missed

    def _observe(
        self, conn: Connection, job: Job, step_key: str, rule: str, outcome: str,
        observed: dict[str, Any], expected: dict[str, Any],
    ) -> None:  # fmt: skip
        conn.execute(
            text(
                "INSERT INTO rai.observations (run_id, step_key, kind, rule, observed_value, "
                "expected, outcome) VALUES (:r, :k, 'metric', :rule, CAST(:o AS jsonb), "
                "CAST(:e AS jsonb), CAST(:out AS rai.observation_outcome)) "
                "ON CONFLICT (run_id, step_key, rule) DO NOTHING"
            ),
            {"r": job.run_id, "k": step_key, "rule": rule, "o": json.dumps(observed),
             "e": json.dumps(expected), "out": outcome},
        )  # fmt: skip

    def _hold(self, job: Job, outcome: FaultOutcome) -> None:
        """Wait until the fault's declared end, renewing the lease; returns at once for a
        fault that has already ended (an instant restart, or a retry after the expiry),
        and early if the agent no longer has it open (the revert then says who ended it).
        An agent that does not answer is waited out: its expiry and the watchdog still
        end the fault."""
        assert outcome.fault is not None  # noqa: S101 - an injected fault has its record
        ends = outcome.fault.get("expires_at")
        if not ends or outcome.fault.get("status") != "active":
            return
        chaos = self.config.chaos
        fault_id = str(outcome.fault.get("fault_id"))
        until = datetime.fromisoformat(str(ends).replace("Z", "+00:00"))
        while (left := (until - datetime.now(UTC)).total_seconds()) > 0:
            if not self.renew(job):
                raise LeaseLost(job.job_id)
            self.sleep(min(left, self.hold_check))
            still = None if chaos is None else chaos.agent.open_fault_ids()
            if still is not None and fault_id not in still:
                return

    def _record_step(
        self,
        conn: Connection,
        job: Job,
        item: PinnedItem,
        started: datetime,
        *,
        state: str,
        verdict: str | None,
        error: str | None,
    ) -> None:
        conn.execute(
            text(
                "INSERT INTO rai.run_steps (run_id, step_key, plan_position, state, verdict, "
                "started_at, error, attempt) VALUES (:r, :k, :p, CAST(:s AS rai.step_state), "
                "CAST(:v AS rai.run_verdict), :t, :e, :a)"
            ),
            {
                "r": job.run_id, "k": item.step_key, "p": item.position, "s": state,
                "v": verdict, "t": started, "e": error[:2000] if error else None,
                "a": job.attempt,
            },
        )  # fmt: skip

    def _step(self, job: Job, step: Prepared) -> None:
        started = datetime.now(UTC)
        definition = step.definition
        source = connector(self.config.connectors[definition.connector])
        assert step.item.asset is not None  # noqa: S101 - checked in preflight
        assert step.item.asset.locator  # noqa: S101 - checked in preflight
        locator = step.item.asset.locator
        observations: list[dict[str, Any]]
        edges: list[dict[str, Any]] = []
        verdict: str | None
        error_text: str | None
        try:
            frame = source.read(locator, environment=definition.environment)
            references = {
                ref: source.read(where, environment=definition.environment)
                for ref, where in definition.references.items()
            }
            upstream = self._read_upstream(step)
        except ConnectorError as error:
            state, verdict, error_text = "error", None, f"could not read the data: {error}"
            observations = [
                {
                    "kind": "note",
                    "rule": "read",
                    "outcome": "error",
                    "rows": None,
                    "failing": None,
                    "observed": json.dumps({"message": error_text}),
                    "expected": "{}",
                }
            ]
        else:
            # Digests of exactly the frames validated below (FR-04.1).
            read = {step.item.asset.asset_id: _digested(frame)} | upstream
            edges = _edges(step.item, read, {ref: _digested(f) for ref, f in references.items()})
            history_dir = self.config.state_dir / str(job.tenant_id)
            history_dir.mkdir(parents=True, exist_ok=True)
            with SqliteKeyStore(history_dir / "history.sqlite") as history:
                report = validate(
                    step.contract, frame, references=references,
                    as_of=started if definition.freshness_reference == "now" else None,
                    history=history, batch_id=locator, record=True,
                )  # fmt: skip
            outcome = decide(report, fail_on=definition.fail_on, subject=step.item.step_key)
            state, verdict, error_text = "done", ("pass" if outcome.passed else "fail"), None
            observations = _observations(report, step, definition.fail_on)
        self._commit_step(job, step.item, started, observations, edges,
                          state=state, verdict=verdict, error=error_text)  # fmt: skip

    def _commit_step(
        self, job: Job, item: PinnedItem, started: datetime, observations: list[dict[str, Any]],
        edges: list[dict[str, Any]], *, state: str, verdict: str | None, error: str | None,
    ) -> None:  # fmt: skip
        """A test step's observations, its step row and its lineage, in one transaction."""
        assert item.asset is not None  # noqa: S101 - checked in preflight
        with tenant_transaction(self.engine, job, self.config.name) as conn:
            if observations:
                conn.execute(
                    text(
                        "INSERT INTO rai.observations (run_id, step_key, test_id, asset_id, kind, "
                        "rule, observed_value, expected, outcome, row_count, failing_count) "
                        "VALUES (:run, :step, :test, :asset, CAST(:kind AS rai.observation_kind), "
                        ":rule, CAST(:observed AS jsonb), CAST(:expected AS jsonb), "
                        "CAST(:outcome AS rai.observation_outcome), :rows, :failing)"
                    ),
                    [
                        o
                        | {
                            "run": job.run_id,
                            "step": item.step_key,
                            "test": item.test.test_id if item.test else None,
                            "asset": item.asset.asset_id,
                        }
                        for o in observations
                    ],
                )
            conn.execute(
                text(
                    "INSERT INTO rai.run_steps (run_id, step_key, plan_position, state, verdict, "
                    "started_at, error, attempt) VALUES (:r, :k, :p, CAST(:s AS rai.step_state), "
                    "CAST(:v AS rai.run_verdict), :t, :e, :a)"
                ),
                {
                    "r": job.run_id, "k": item.step_key, "p": item.position, "s": state,
                    "v": verdict, "t": started, "e": error[:2000] if error else None,
                    "a": job.attempt,
                },
            )  # fmt: skip
            if edges:  # after the step row, which they reference
                conn.execute(
                    text(
                        "INSERT INTO rai.lineage_edges (run_id, step_key, from_asset_id, "
                        "from_name, to_asset_id, from_digest, to_digest, from_rows, transform_ref) "
                        "VALUES (:run, :step, :from_asset, :from_name, :to_asset, :from_digest, "
                        ":to_digest, :from_rows, :transform)"
                    ),
                    [e | {"run": job.run_id, "step": item.step_key} for e in edges],
                )
            if self.before_commit is not None:
                self.before_commit(item.step_key)

    # ---- consistency checks (FR-08.3) ---------------------------------------------------------

    def _consistency(self, job: Job, step: PreparedConsistency) -> None:
        started = datetime.now(UTC)
        definition = step.definition
        environment = definition.environment
        assert step.item.asset is not None  # noqa: S101 - checked in preflight
        assert step.item.asset.locator  # noqa: S101 - checked in preflight
        assert step.source.locator  # noqa: S101 - checked in preflight
        target_reader = connector(self.config.connectors[definition.connector])
        source_reader = connector(self.config.connectors[definition.source.connector])
        target_at, source_at = step.item.asset.locator, step.source.locator
        rounds = 0
        try:
            snapshot = prepare_source(source_reader.read(source_at, environment=environment),
                                      definition)  # fmt: skip
            while True:
                rounds += 1
                target_raw = target_reader.read(target_at, environment=environment)
                source_raw = source_reader.read(source_at, environment=environment)
                result = reconcile(
                    snapshot, prepare_source(source_raw, definition),
                    prepare_target(target_raw, definition),
                    key=definition.key, compare=definition.compare,
                    allow_unexpected=definition.allow_unexpected,
                )  # fmt: skip
                elapsed = (datetime.now(UTC) - started).total_seconds()
                if result.passed or elapsed + definition.interval_seconds > (
                    definition.within_seconds
                ):
                    break
                if not self.renew(job):
                    raise LeaseLost(job.job_id)
                self.sleep(definition.interval_seconds)
        except (ConnectorError, ValueError) as error:
            message = f"could not reconcile: {error}"
            note = {"kind": "note", "rule": "read", "outcome": "error", "rows": None,
                    "failing": None, "observed": json.dumps({"message": message}),
                    "expected": "{}"}  # fmt: skip
            self._commit_step(job, step.item, started, [note], [],
                              state="error", verdict=None, error=message)  # fmt: skip
            return
        seconds = round((datetime.now(UTC) - started).total_seconds(), 3)
        observations = _consistency_observations(result, definition, rounds, seconds)
        problems = [] if result.passed else [
            f"not consistent after {rounds} round(s) in {seconds} s: {result.summary()}"
        ]  # fmt: skip
        if definition.rpo is not None:
            try:
                truth_obs, truth_problems = self._ground_truth(
                    job, definition, snapshot, prepare_target(target_raw, definition)
                )
            except ValueError as error:
                truth_obs = None
                truth_problems = [f"RPO ground truth: {error}"]
            if truth_obs is not None:
                observations.append(truth_obs)
            problems += truth_problems
        read = {step.source.asset_id: _digested(source_raw),
                step.item.asset.asset_id: _digested(target_raw)}  # fmt: skip
        self._commit_step(job, step.item, started, observations, _edges(step.item, read, {}),
                          state="done", verdict="fail" if problems else "pass",
                          error="; ".join(problems) or None)  # fmt: skip

    def _drift(self, job: Job, step: PreparedDrift) -> None:
        """Compare the target (current) with the pinned reference, column by column
        (FR-09.2). Any alert, or a column that cannot be judged, fails the step."""
        started = datetime.now(UTC)
        definition = step.definition
        environment = definition.environment
        assert step.item.asset is not None  # noqa: S101 - checked in preflight
        assert step.item.asset.locator  # noqa: S101 - checked in preflight
        assert step.reference.locator  # noqa: S101 - checked in preflight
        try:
            current = connector(self.config.connectors[definition.connector]).read(
                step.item.asset.locator, environment=environment
            )
            reference = connector(self.config.connectors[definition.reference.connector]).read(
                step.reference.locator, environment=environment
            )
        except (ConnectorError, ValueError) as error:
            message = f"could not read the data to compare: {error}"
            note = {"kind": "note", "rule": "read", "outcome": "error", "rows": None,
                    "failing": None, "observed": json.dumps({"message": message}),
                    "expected": "{}"}  # fmt: skip
            self._commit_step(job, step.item, started, [note], [],
                              state="error", verdict=None, error=message)  # fmt: skip
            return
        results = drift(reference, current, definition)
        problems = [f"{r.column}: {r.error}" for r in results if r.error] + [
            f"{r.column}: {'; '.join(r.alerts)}" for r in results if r.alerts
        ]
        read = {step.item.asset.asset_id: _digested(current)}
        references = {step.reference.name: _digested(reference)}
        self._commit_step(
            job, step.item, started, _drift_observations(results, definition),
            _edges(step.item, read, references),
            state="done", verdict="fail" if problems else "pass",
            error="drift: " + " | ".join(problems) if problems else None,
        )  # fmt: skip

    def _metric(self, job: Job, step: PreparedMetric) -> None:
        """Evaluate a model's scores against their labels (FR-09.1). A missed target,
        a metric that cannot be judged, or too little data fails the step."""
        started = datetime.now(UTC)
        definition = step.definition
        assert step.item.asset is not None  # noqa: S101 - checked in preflight
        assert step.item.asset.locator  # noqa: S101 - checked in preflight
        try:
            scores = connector(self.config.connectors[definition.connector]).read(
                step.item.asset.locator, environment=definition.environment
            )
        except (ConnectorError, ValueError) as error:
            message = f"could not read the scores: {error}"
            note = {"kind": "note", "rule": "read", "outcome": "error", "rows": None,
                    "failing": None, "observed": json.dumps({"message": message}),
                    "expected": "{}"}  # fmt: skip
            self._commit_step(job, step.item, started, [note], [],
                              state="error", verdict=None, error=message)  # fmt: skip
            return
        result = evaluate(scores, definition)
        problems = result.problems
        read = {step.item.asset.asset_id: _digested(scores)}
        self._commit_step(
            job, step.item, started, _metric_observations(result, definition),
            _edges(step.item, read, {}),
            state="done", verdict="fail" if problems else "pass",
            error="model evaluation: " + " | ".join(problems) if problems else None,
        )  # fmt: skip

    def _canary(self, job: Job, step: PreparedCanary) -> None:
        """Search the target for every canary value of the pinned key (FR-05.1). Any
        hit fails the step; a key that cannot work is an error. No value is ever
        recorded in full: hits are masked."""
        started = datetime.now(UTC)
        definition = step.definition
        environment = definition.environment
        assert step.item.asset is not None  # noqa: S101 - checked in preflight
        assert step.item.asset.locator  # noqa: S101 - checked in preflight
        assert step.key.locator  # noqa: S101 - checked in preflight
        try:
            target = connector(self.config.connectors[definition.connector]).read(
                step.item.asset.locator, environment=environment
            )
            key = connector(self.config.connectors[definition.key.connector]).read(
                step.key.locator, environment=environment
            )
        except (ConnectorError, ValueError) as error:
            message = f"could not read the data to search: {error}"
            note = {"kind": "note", "rule": "read", "outcome": "error", "rows": None,
                    "failing": None, "observed": json.dumps({"message": message}),
                    "expected": "{}"}  # fmt: skip
            self._commit_step(job, step.item, started, [note], [],
                              state="error", verdict=None, error=message)  # fmt: skip
            return
        found, problem = needles(key)
        result = (Scan(target.height, (), 0, 0, error=problem) if problem
                  else scan(target, found, definition))  # fmt: skip
        read = {step.item.asset.asset_id: _digested(target)}
        keys = {step.key.name: _digested(key)}
        self._commit_step(
            job, step.item, started, [_canary_observation(result, definition)],
            _edges(step.item, read, keys),
            state="done", verdict="pass" if result.outcome == "pass" else "fail",
            error=None if result.outcome == "pass" else f"privacy: {result.summary()}",
        )  # fmt: skip

    def _ground_truth(
        self, job: Job, definition: ConsistencyDefinition, acked: pl.DataFrame,
        ledger: pl.DataFrame,
    ) -> tuple[dict[str, Any], list[str]]:  # fmt: skip
        """recovery.rpo_truth (B-30): what the seeding log says the ledger lost, dated
        from the run's first fault onset, beside the ledger's own account (B-26b)."""
        assert definition.rpo is not None  # noqa: S101 - the caller checks
        with tenant_transaction(self.engine, job, self.config.name) as conn:
            onsets = conn.execute(
                text("SELECT observed_value->>'onset' FROM rai.observations "
                     "WHERE run_id = :r AND rule = 'fault.onset'"), {"r": job.run_id},
            ).scalars().all()  # fmt: skip
            measured = conn.execute(
                text("SELECT observed_value FROM rai.observations WHERE run_id = :r "
                     "AND rule = 'recovery.rpo' ORDER BY observed_at LIMIT 1"),
                {"r": job.run_id},
            ).scalar_one_or_none()  # fmt: skip
        moments = [m for m in (_moment(o) for o in onsets) if m is not None]
        truth = ground_truth(acked, ledger, key=definition.key, at=definition.rpo.at,
                             onset=min(moments) if moments else None)  # fmt: skip
        problems = _truth_problems(truth, definition.rpo.objective_seconds, measured)
        observed = {
            "seconds": truth.seconds, "lost_acknowledged": truth.lost,
            "earliest_lost_at": _stamp(truth.earliest_lost_at), "onset": _stamp(truth.onset),
            "acknowledged": acked.height, "sample": list(truth.sample),
            "measured": None if measured is None else {
                "seconds": measured.get("seconds"), "lost_rows": measured.get("lost_rows"),
            },
        }  # fmt: skip
        expected = {"lost_acknowledged": 0, "objective_seconds": definition.rpo.objective_seconds}
        return {
            "kind": "metric", "rule": "recovery.rpo_truth",
            "outcome": "fail" if problems else "pass", "rows": acked.height,
            "failing": truth.lost, "observed": json.dumps(observed, default=str),
            "expected": json.dumps(expected),
        }, problems  # fmt: skip

    def _read_upstream(self, step: Prepared) -> dict[UUID, Digested]:
        """Read and digest the upstream assets the definition maps to a connector."""
        environment = step.definition.environment
        sources = {hop.source.name: hop.source for hop in step.item.upstream}
        digested: dict[UUID, Digested] = {}
        for name, connector_name in sorted(step.definition.upstream.items()):
            asset = sources[name]  # checked in preflight
            assert asset.locator  # noqa: S101 - checked in preflight
            reader = connector(self.config.connectors[connector_name])
            digested[asset.asset_id] = _digested(
                reader.read(asset.locator, environment=environment)
            )
        return digested

    def _decide(self, job: Job, pins: Pins) -> None:
        """passed only if every planned step is recorded, done, and passed."""
        with tenant_transaction(self.engine, job, self.config.name) as conn:
            steps = conn.execute(
                text("SELECT state::text, verdict::text FROM rai.run_steps WHERE run_id = :r"),
                {"r": job.run_id},
            ).all()
        complete = len(steps) == len(pins.items)
        passed = complete and all(s.state == "done" and s.verdict == "pass" for s in steps)
        move(self.engine, job, self.config.name, "passed" if passed else "failed",
             verdict="pass" if passed else "fail")  # fmt: skip


Step = (
    Prepared
    | PreparedScenario
    | PreparedConsistency
    | PreparedDrift
    | PreparedMetric
    | PreparedCanary
)


def _truth_problems(
    truth: GroundTruth, objective: int | None, measured: dict[str, Any] | None
) -> list[str]:
    problems = []
    if truth.lost:
        problems.append(f"{truth.lost} acknowledged write(s) are not in the ledger "
                        "(seeding log)")  # fmt: skip
    if objective is not None and truth.seconds is not None and truth.seconds > objective:
        problems.append(f"ground-truth RPO {truth.seconds:.1f} s exceeds the objective of "
                        f"{objective} s")  # fmt: skip
    if measured is not None:
        reported = int(measured.get("lost_rows") or 0)
        if (reported == 0) != (truth.lost == 0):
            problems.append(
                f"the ledger's own head reported {reported} lost row(s), but the seeding log "
                f"shows {truth.lost} lost acknowledged write(s)"
            )
    return problems


def _drift_observations(
    results: list[ColumnDrift], definition: DriftDefinition
) -> list[dict[str, Any]]:
    """One metric per column: PSI, KS and both sample sizes; ``fail`` on an alert."""
    limits = {c.name: c for c in definition.columns}
    return [
        {
            "kind": "metric", "rule": f"drift.{r.column}", "outcome": r.outcome,
            "rows": r.current_rows, "failing": None,
            "observed": json.dumps({
                "kind": r.kind, "psi": r.psi, "band": r.band, "ks_statistic": r.ks_statistic,
                "ks_p_value": r.ks_p_value, "reference_rows": r.reference_rows,
                "current_rows": r.current_rows,
                "reference_null_share": r.reference_null_share,
                "current_null_share": r.current_null_share, "alerts": list(r.alerts),
                "error": r.error, "bins": r.bins,
            }, default=str),
            "expected": json.dumps({
                "psi_alert": limits[r.column].psi_alert, "ks_alert": limits[r.column].ks_alert,
                "bins": definition.bins, "min_rows": definition.min_rows,
                "reference_asset": definition.reference.asset,
            }),
        }
        for r in results
    ]  # fmt: skip


def _metric_observations(result: Evaluation, definition: MetricDefinition) -> list[dict[str, Any]]:
    """``tevv.sample``, the confusion matrix and the rows behind it, then one metric
    per target with its counts, interval and target (FR-09.1: n always)."""
    confusion = result.confusion
    sample = {
        "kind": "metric", "rule": "tevv.sample", "outcome": "error" if result.error else "pass",
        "rows": result.rows_read, "failing": None,
        "observed": json.dumps({
            "rows_read": result.rows_read, "null_rows": result.null_rows,
            "rows_used": confusion.n if confusion else 0,
            "positives": confusion.positives if confusion else None,
            "negatives": confusion.negatives if confusion else None,
            "flagged": confusion.flagged if confusion else None,
            "confusion": confusion.as_dict() if confusion else None, "error": result.error,
        }),
        "expected": json.dumps({
            "threshold": definition.threshold, "score_column": definition.score_column,
            "label_column": definition.label_column, "min_rows": definition.min_rows,
            "min_positives": definition.min_positives,
            "min_negatives": definition.min_negatives,
        }),
    }  # fmt: skip
    metrics = [
        {
            "kind": "metric", "rule": f"tevv.{r.name}", "outcome": r.outcome,
            "rows": confusion.n if confusion else None, "failing": None,
            "observed": json.dumps({
                "value": r.value, "numerator": r.numerator, "denominator": r.denominator,
                "low": r.low, "high": r.high, "confidence": definition.confidence,
                "counts": describe(r.name), "problem": r.problem, "error": r.error,
            }),
            "expected": json.dumps({
                "at_least": r.target.at_least, "at_most": r.target.at_most,
                "judge_on": definition.judge_on, "threshold": definition.threshold,
            }),
        }
        for r in result.results
    ]  # fmt: skip
    return [sample, *metrics]


def _canary_observation(result: Scan, definition: CanaryDefinition) -> dict[str, Any]:
    """``privacy.canary``: what was searched, how many hits by kind, column and canary,
    and a sample of masked hits. Never a needle in full."""
    return {
        "kind": "rule_result", "rule": "privacy.canary", "outcome": result.outcome,
        "rows": result.rows, "failing": result.hit_rows,
        "observed": json.dumps({
            "hits": result.hits, "hit_rows": result.hit_rows,
            "columns_searched": list(result.columns), "needles": result.needles,
            "canaries": result.canaries, "by_kind": result.by_kind,
            "by_column": result.by_column, "by_canary": result.by_canary,
            "sample": [h.as_dict() for h in result.sample], "error": result.error,
        }),
        "expected": json.dumps({
            "hits": 0, "key_asset": definition.key.asset,
            "case_sensitive": definition.case_sensitive,
        }),
    }  # fmt: skip


def _consistency_observations(
    result: Reconciliation, definition: ConsistencyDefinition, rounds: int, seconds: float
) -> list[dict[str, Any]]:
    expected = json.dumps({"count": 0, "key": list(definition.key),
                           "compare": list(definition.compare),
                           "within_seconds": definition.within_seconds})  # fmt: skip
    return [
        {
            "kind": "rule_result", "rule": f"consistency.{finding.rule}",
            "outcome": finding.outcome, "rows": result.target_rows,
            "failing": finding.count,
            "observed": json.dumps({
                "count": finding.count, "sample": list(finding.sample), "rounds": rounds,
                "seconds": seconds, "snapshot_rows": result.snapshot_rows,
                "source_rows": result.source_rows, "target_rows": result.target_rows,
            }, default=str),
            "expected": expected,
        }
        for finding in result.findings
    ]  # fmt: skip


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _head_dict(head: Head) -> dict[str, Any]:
    return {"seq": head.seq, "rows": head.rows, "committed_at": _stamp(head.committed_at)}


def _head_json(head: Head | None) -> dict[str, Any] | None:
    return None if head is None else _head_dict(head)


def _record_measurement(
    conn: Connection, job: Job, step: PreparedScenario, row: dict[str, Any]
) -> None:
    """The step's recovery measurement (B-29), insert-only and committed with the step."""
    scenario = step.item.scenario
    assert scenario is not None  # noqa: S101 - a scenario step
    conn.execute(
        text(
            "INSERT INTO rai.recovery_measurements (run_id, step_key, scenario_id, onset_at, "
            "reverted_at, restored_at, rto_seconds, rto_objective_seconds, rto_met, "
            "rpo_attempted, rpo_seconds, rpo_objective_seconds, rpo_met, lost_rows, "
            "stable_checks, window_seconds) VALUES (:r, :k, :s, :onset_at, :reverted_at, "
            ":restored_at, :rto_seconds, :rto_objective_seconds, :rto_met, :rpo_attempted, "
            ":rpo_seconds, :rpo_objective_seconds, :rpo_met, :lost_rows, :stable_checks, "
            ":window_seconds)"
        ),
        {"r": job.run_id, "k": step.item.step_key, "s": scenario.scenario_id, **row},
    )
