"""Assurance runs: start, list, inspect (B-18: FR-06.1, FR-06.2, FR-11.1).

``POST /v1/assurance/runs`` pins every input of the plan version, writes the run
(state ``queued``) and its job in one transaction, and answers 202 with the run.
With an ``Idempotency-Key``, sending the same request again returns the same run;
reusing the key for a different request is refused (422).

**API keys (B-20).** A project API key with scope ``runs:start`` may start runs of
its own project's plans, by id or by name (the newest version, as ``rai run
--plan ci-contracts`` does); with ``runs:read`` it may read its project's runs.
Runs of other projects are 404 to it. The run records the key, and its creator as
``triggered_by``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from rai import __version__
from rai.api.context import ROLES, TenantSession, require
from rai.api.listing import Page, PageOut, after_uuid, page, paged
from rai.api.problems import Problem
from rai.api.routes_projects import find_project, is_unique_violation
from rai.core.runs import (
    PinnedAsset,
    PinnedContract,
    PinnedEnvironment,
    PinnedInput,
    PinnedItem,
    PinnedPlan,
    PinnedTest,
    Pins,
    RunState,
    step_key,
)
from rai.lineage.digest import DIGEST_FORMAT

router = APIRouter(prefix="/v1/assurance/runs", tags=["runs"])

anyone = require(*ROLES, key_scope="runs:read")
starter = require("engineer", key_scope="runs:start")
MAX_UPSTREAM = 100  # declared hops pinned per plan item


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID | None = None
    plan: str | None = Field(
        default=None, min_length=1, max_length=200, description="a plan name: its newest version"
    )
    project_id: UUID | None = Field(
        default=None, description="with plan: the project (an API key implies its own)"
    )
    trigger_ref: str | None = Field(default=None, max_length=200, description="e.g. a commit SHA")

    @model_validator(mode="after")
    def _one_plan(self) -> RunIn:
        if (self.plan_id is None) == (self.plan is None):
            raise ValueError("give exactly one of plan_id or plan")
        return self


class RunOut(BaseModel):
    run_id: UUID
    project_id: UUID
    plan_id: UUID
    environment_id: UUID | None
    state: RunState
    verdict: Literal["pass", "fail"] | None
    reason: str | None
    triggered_by: UUID
    api_key_id: UUID | None
    trigger_ref: str | None
    rai_version: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class StepOut(BaseModel):
    step_key: str
    plan_position: int
    state: Literal["done", "error"]
    verdict: Literal["pass", "fail"] | None
    error: str | None
    attempt: int
    started_at: datetime
    finished_at: datetime


class RunDetail(RunOut):
    pins: dict[str, Any]
    steps: list[StepOut]
    observations: dict[str, int]  # outcome -> count


class ObservationOut(BaseModel):
    observation_id: UUID
    step_key: str
    test_id: UUID | None
    asset_id: UUID | None
    kind: str
    rule: str
    outcome: Literal["pass", "fail", "error", "skipped"]
    row_count: int | None
    failing_count: int | None
    observed_value: dict[str, Any]
    expected: dict[str, Any]
    observed_at: datetime


_RUN = (
    "SELECT run_id, project_id, plan_id, environment_id, state::text, verdict::text, reason, "
    "triggered_by, api_key_id, trigger_ref, rai_version, queued_at, started_at, finished_at "
    "FROM rai.runs"
)


def _upstream(conn: Connection, asset_id: UUID, position: int) -> tuple[PinnedInput, ...]:
    """Every declared hop upstream of an asset (the graph is acyclic: a trigger keeps it so)."""
    rows = conn.execute(
        text(
            "WITH RECURSIVE hops AS ("
            " SELECT asset_id, input_asset_id, transform_ref FROM rai.asset_inputs"
            " WHERE asset_id = :a"
            " UNION"
            " SELECT i.asset_id, i.input_asset_id, i.transform_ref FROM rai.asset_inputs i"
            " JOIN hops h ON i.asset_id = h.input_asset_id)"
            " SELECT h.asset_id AS into_id, h.transform_ref, p.asset_id, p.name, p.locator,"
            " p.kind::text AS kind FROM hops h"
            " JOIN rai.pipeline_assets p ON p.asset_id = h.input_asset_id"
            " ORDER BY h.asset_id, p.name LIMIT :n"
        ),
        {"a": asset_id, "n": MAX_UPSTREAM + 1},
    ).all()
    if len(rows) > MAX_UPSTREAM:
        raise Problem(
            422, "runs.lineage_too_large", "The target asset's upstream graph is too large",
            detail=f"plan item {position}: more than {MAX_UPSTREAM} declared inputs upstream",
        )  # fmt: skip
    return tuple(
        PinnedInput(
            source=PinnedAsset(asset_id=r.asset_id, name=r.name, locator=r.locator, kind=r.kind),
            into=r.into_id,
            transform_ref=r.transform_ref,
        )
        for r in rows
    )


def _plan_id(session: TenantSession, body: RunIn) -> UUID:
    """The plan to run: by id, or the newest version of a plan name in a project."""
    if body.plan_id is not None:
        return body.plan_id
    project_id = body.project_id or (session.key.project_id if session.key else None)
    if project_id is None:
        raise Problem(422, "runs.project_required", "Give project_id with a plan name")
    found = session.conn.execute(
        text(
            "SELECT plan_id FROM rai.test_plans WHERE project_id = :p AND name = :n "
            "ORDER BY version DESC LIMIT 1"
        ),
        {"p": project_id, "n": body.plan},
    ).scalar_one_or_none()
    if found is None or not session.own_project(project_id):
        raise Problem(404, "plans.not_found", "Plan not found")
    plan_id: UUID = found
    return plan_id


def _pins(session: TenantSession, plan_id: UUID) -> tuple[UUID, Pins]:
    conn = session.conn
    plan = conn.execute(
        text("SELECT plan_id, project_id, name, version FROM rai.test_plans WHERE plan_id = :p"),
        {"p": plan_id},
    ).first()
    if plan is None or not session.own_project(plan.project_id):
        raise Problem(404, "plans.not_found", "Plan not found")
    find_project(conn, plan.project_id, for_change=True)
    rows = conn.execute(
        text(
            "SELECT i.position, i.contract_id, t.test_id, t.name, t.version, t.type::text, "
            "t.severity::text, t.definition, t.target_asset_id "
            "FROM rai.plan_items i JOIN rai.test_cases t USING (test_id) "
            "WHERE i.plan_id = :p ORDER BY i.position"
        ),
        {"p": plan_id},
    ).all()
    items = []
    for row in rows:
        asset = contract = None
        upstream: tuple[PinnedInput, ...] = ()
        if row.target_asset_id is not None:
            found = conn.execute(
                text(
                    "SELECT asset_id, name, locator, kind::text FROM rai.pipeline_assets "
                    "WHERE asset_id = :a"
                ),
                {"a": row.target_asset_id},
            ).one()
            asset = PinnedAsset.model_validate(found._asdict())
            upstream = _upstream(conn, row.target_asset_id, row.position)
        if row.contract_id is not None or row.type == "contract_rule":
            chosen = conn.execute(
                text(
                    "SELECT contract_id, name || '@' || version AS ref, spec_sha256 "
                    "FROM rai.data_contracts WHERE "
                    + ("contract_id = :c" if row.contract_id else "asset_id = :a")
                    + " ORDER BY version DESC LIMIT 1"
                ),
                {"c": row.contract_id, "a": row.target_asset_id},
            ).first()
            if chosen is None:
                raise Problem(
                    422, "runs.no_contract", "A contract_rule test needs a published contract",
                    detail=f"plan item {row.position}: publish a contract for its asset first",
                )  # fmt: skip
            contract = PinnedContract(
                contract_id=chosen.contract_id, ref=chosen.ref, spec_sha256=chosen.spec_sha256
            )
        test = PinnedTest(
            test_id=row.test_id, name=row.name, version=row.version, type=row.type,
            severity=row.severity, definition=row.definition,
        )  # fmt: skip
        items.append(
            PinnedItem(
                position=row.position,
                step_key=step_key(row.position),
                test=test,
                asset=asset,
                contract=contract,
                upstream=upstream,
            )
        )
    pinned = PinnedPlan(plan_id=plan.plan_id, name=plan.name, version=plan.version)
    return plan.project_id, Pins(
        rai_version=__version__, plan=pinned, items=items,
        environment=_environment(conn, plan.plan_id),
    )  # fmt: skip


def _environment(conn: Connection, plan_id: UUID) -> PinnedEnvironment | None:
    """The plan's environment, else its project's default (B-22). A retired one is refused:
    nothing new runs there."""
    row = conn.execute(
        text(
            "SELECT e.environment_id, e.name, e.type::text, e.status::text "
            "FROM rai.test_plans p JOIN rai.assurance_projects pr USING (project_id) "
            "JOIN rai.environments e "
            "ON e.environment_id = COALESCE(p.environment_id, pr.default_environment_id) "
            "WHERE p.plan_id = :p"
        ),
        {"p": plan_id},
    ).first()
    if row is None:
        return None
    if row.status != "active":
        raise Problem(
            409, "runs.environment_retired", "The run's environment is retired",
            detail=f"environment {row.name}: choose an active one for the plan or project",
        )  # fmt: skip
    return PinnedEnvironment(environment_id=row.environment_id, name=row.name, type=row.type)


def _request_hash(body: RunIn) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replay(session: TenantSession, key: str, digest: str) -> RunOut | None:
    row = session.conn.execute(
        text(f"{_RUN.replace(' FROM', ', request_sha256 FROM')} WHERE idempotency_key = :k"),
        {"k": key},
    ).first()
    if row is None:
        return None
    if row.request_sha256 != digest or not session.own_project(row.project_id):
        raise Problem(
            422, "idempotency.key_reused", "This Idempotency-Key was used for a different request"
        )
    data = row._asdict()
    data.pop("request_sha256")
    return RunOut.model_validate(data)


def find_run(session: TenantSession, run_id: UUID) -> RunOut:
    """The run, or 404 (another tenant's, or for an API key another project's)."""
    row = session.conn.execute(text(f"{_RUN} WHERE run_id = :r"), {"r": run_id}).first()
    if row is None or not session.own_project(row.project_id):
        raise Problem(404, "runs.not_found", "Run not found")
    return RunOut.model_validate(row._asdict())


@router.post("", status_code=202)
def start_run(
    body: RunIn,
    response: Response,
    session: Annotated[TenantSession, Depends(starter)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)
    ] = None,
) -> RunOut:
    digest = _request_hash(body)
    if idempotency_key is not None:
        earlier = _replay(session, idempotency_key, digest)
        if earlier is not None:
            response.headers["Location"] = f"/v1/assurance/runs/{earlier.run_id}"
            return earlier
    project_id, pins = _pins(session, _plan_id(session, body))
    try:
        with session.conn.begin_nested():
            run_id = session.conn.execute(
                text(
                    "INSERT INTO rai.runs (project_id, plan_id, environment_id, triggered_by, "
                    "api_key_id, trigger_ref, idempotency_key, request_sha256, rai_version, "
                    "pins) VALUES (:p, :plan, :env, :u, :key, :ref, :k, :h, :v, "
                    "CAST(:pins AS jsonb)) RETURNING run_id"
                ),
                {
                    "p": project_id, "plan": pins.plan.plan_id, "u": session.user_id,
                    "env": pins.environment.environment_id if pins.environment else None,
                    "key": session.key.api_key_id if session.key else None,
                    "ref": body.trigger_ref, "k": idempotency_key, "h": digest,
                    "v": pins.rai_version, "pins": pins.model_dump_json(),
                },
            ).scalar_one()  # fmt: skip
    except IntegrityError as error:
        if idempotency_key is None or not is_unique_violation(error):
            raise
        replayed = _replay(session, idempotency_key, digest)  # a concurrent twin won
        assert replayed is not None  # noqa: S101 - the unique key says it exists
        response.headers["Location"] = f"/v1/assurance/runs/{replayed.run_id}"
        return replayed
    session.conn.execute(text("INSERT INTO rai.jobs (run_id) VALUES (:r)"), {"r": run_id})
    response.headers["Location"] = f"/v1/assurance/runs/{run_id}"
    return find_run(session, run_id)


@router.get("")
def list_runs(
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
    project_id: Annotated[UUID | None, Query()] = None,
    state: Annotated[RunState | None, Query()] = None,
    since: Annotated[datetime | None, Query(description="queued at or after (ISO 8601)")] = None,
) -> PageOut[RunOut]:
    """Newest first. An API key sees only its own project's runs."""
    if session.key is not None:
        if project_id not in (None, session.key.project_id):
            return PageOut[RunOut](items=[], next_cursor=None)
        project_id = session.key.project_id
    after = after_uuid(wanted)
    rows = session.conn.execute(
        text(
            f"{_RUN} WHERE (CAST(:p AS uuid) IS NULL OR project_id = :p) "
            "AND (CAST(:s AS text) IS NULL OR state::text = :s) "
            "AND (CAST(:since AS timestamptz) IS NULL OR queued_at >= :since) "
            "AND (CAST(:after AS uuid) IS NULL OR run_id < :after) "
            "ORDER BY run_id DESC LIMIT :n"
        ),
        {"p": project_id, "s": state, "since": since, "after": after, "n": wanted.limit + 1},
    ).all()
    runs = [RunOut.model_validate(r._asdict()) for r in rows]
    return paged(runs, wanted, lambda r: str(r.run_id))


@router.get("/{run_id}")
def get_run(run_id: UUID, session: Annotated[TenantSession, Depends(anyone)]) -> RunDetail:
    run = find_run(session, run_id)
    pins = session.conn.execute(
        text("SELECT pins FROM rai.runs WHERE run_id = :r"), {"r": run_id}
    ).scalar_one()
    steps = session.conn.execute(
        text(
            "SELECT step_key, plan_position, state::text, verdict::text, error, attempt, "
            "started_at, finished_at FROM rai.run_steps WHERE run_id = :r ORDER BY plan_position"
        ),
        {"r": run_id},
    ).all()
    counts = session.conn.execute(
        text(
            "SELECT outcome::text, count(*) FROM rai.observations WHERE run_id = :r "
            "GROUP BY outcome ORDER BY outcome"
        ),
        {"r": run_id},
    ).all()
    return RunDetail(
        **run.model_dump(),
        pins=pins,
        steps=[StepOut.model_validate(s._asdict()) for s in steps],
        observations={outcome: int(n) for outcome, n in counts},
    )


@router.get("/{run_id}/observations")
def list_observations(
    run_id: UUID,
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
    outcome: Annotated[Literal["pass", "fail", "error", "skipped"] | None, Query()] = None,
) -> PageOut[ObservationOut]:
    find_run(session, run_id)
    after = after_uuid(wanted)
    rows = session.conn.execute(
        text(
            "SELECT observation_id, step_key, test_id, asset_id, kind::text, rule, "
            "outcome::text, row_count, failing_count, observed_value, expected, observed_at "
            "FROM rai.observations WHERE run_id = :r "
            "AND (CAST(:o AS text) IS NULL OR outcome::text = :o) "
            "AND (CAST(:after AS uuid) IS NULL OR observation_id > :after) "
            "ORDER BY observation_id LIMIT :n"
        ),
        {"r": run_id, "o": outcome, "after": after, "n": wanted.limit + 1},
    ).all()
    items = [ObservationOut.model_validate(r._asdict()) for r in rows]
    return paged(items, wanted, lambda o: str(o.observation_id))


class LineageAsset(BaseModel):
    asset_id: UUID
    name: str
    kind: str


class LineageEdgeOut(BaseModel):
    step_key: str
    from_asset_id: UUID | None = Field(description="null: a reference dataset of the step")
    from_name: str
    from_digest: str | None = Field(description="null: this run did not read it")
    from_rows: int | None
    to_asset_id: UUID | None = Field(description="null: the run itself")
    to_digest: str | None
    transform_ref: str | None
    recorded_at: datetime


class LineageOut(BaseModel):
    run_id: UUID
    digest_format: str
    assets: list[LineageAsset]
    edges: list[LineageEdgeOut]


@router.get("/{run_id}/lineage")
def get_lineage(run_id: UUID, session: Annotated[TenantSession, Depends(anyone)]) -> LineageOut:
    """Every hop each step recorded, with the SHA-256 of the data the worker read."""
    find_run(session, run_id)
    edges = session.conn.execute(
        text(
            "SELECT e.step_key, e.from_asset_id, e.from_name, e.from_digest, e.from_rows, "
            "e.to_asset_id, e.to_digest, e.transform_ref, e.recorded_at "
            "FROM rai.lineage_edges e JOIN rai.run_steps s USING (run_id, step_key) "
            "WHERE e.run_id = :r ORDER BY s.plan_position, e.edge_id"
        ),
        {"r": run_id},
    ).all()
    assets = session.conn.execute(
        text(
            "SELECT asset_id, name, kind::text FROM rai.pipeline_assets WHERE asset_id IN ("
            " SELECT from_asset_id FROM rai.lineage_edges WHERE run_id = :r"
            " UNION SELECT to_asset_id FROM rai.lineage_edges WHERE run_id = :r"
            ") ORDER BY name"
        ),
        {"r": run_id},
    ).all()
    return LineageOut(
        run_id=run_id,
        digest_format=DIGEST_FORMAT,
        assets=[LineageAsset.model_validate(a._asdict()) for a in assets],
        edges=[LineageEdgeOut.model_validate(e._asdict()) for e in edges],
    )
