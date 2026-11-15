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
"""

from __future__ import annotations

import json
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
from rai.core.runs import TERMINAL, PinnedItem, Pins
from rai.core.tenancy import set_context
from rai.lineage.digest import frame_digest
from rai.validation.contract import Contract
from rai.validation.engine import ValidationReport, validate
from rai.validation.history import SqliteKeyStore
from rai.validation.verdict import decide
from rai.worker.queue import Job

SUPPORTED_TYPES = ("contract_rule",)


class LeaseLost(Exception):
    """Another worker took the job over; stop without touching the run."""


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


@dataclass(frozen=True)
class Prepared:
    """A contract-rule step, checked in preflight."""

    item: PinnedItem
    definition: ContractRuleDefinition
    contract: Contract


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


def preflight(
    conn: Connection, pins: Pins, config: WorkerConfig
) -> tuple[dict[str, Prepared], list[str]]:
    """Check every item can run here; returns the prepared steps and the problems."""
    prepared: dict[str, Prepared] = {}
    problems: list[str] = []
    for item in pins.items:
        where = f"item {item.position} ({item.test.name} v{item.test.version})"
        if item.test.type not in SUPPORTED_TYPES:
            problems.append(f"{where}: test type {item.test.type} is not supported yet")
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
    step: Prepared, read: dict[UUID, Digested], references: dict[str, Digested]
) -> list[dict[str, Any]]:
    """Every pinned hop upstream of the target, the target into the run, and each
    reference dataset into the run; digests where this step read the data."""
    target = step.item.asset
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
        for hop in step.item.upstream
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
    ) -> None:
        self.engine, self.config, self.renew = engine, config, renew
        self.before_commit = before_commit  # test hook: called inside a step's transaction

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
            prepared, problems = preflight(conn, pins, self.config)
        if row.state in ("queued", "preflight"):
            if problems:
                move(self.engine, job, name, "refused", reason="; ".join(problems)[:2000])
                return
            move(self.engine, job, name, "running")
        if row.state in ("queued", "preflight", "running"):
            self._steps(job, pins, prepared)
            move(self.engine, job, name, "evaluating")
        self._decide(job, pins)

    def _steps(self, job: Job, pins: Pins, prepared: dict[str, Prepared]) -> None:
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
            self._step(job, step)

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
            edges = _edges(step, read, {ref: _digested(f) for ref, f in references.items()})
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
                            "step": step.item.step_key,
                            "test": step.item.test.test_id,
                            "asset": step.item.asset.asset_id,
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
                    "r": job.run_id, "k": step.item.step_key, "p": step.item.position,
                    "s": state, "v": verdict, "t": started, "e": error_text, "a": job.attempt,
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
                    [e | {"run": job.run_id, "step": step.item.step_key} for e in edges],
                )
            if self.before_commit is not None:
                self.before_commit(step.item.step_key)

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
