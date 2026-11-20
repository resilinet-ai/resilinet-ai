"""Versioned definitions: data contracts, test cases and test plans (B-17: FR-03.1,
FR-03.4, FR-06.1).

Each is immutable once published; publishing again creates the next version, and
a run pins the exact version (Data Model section 0). Engineers write; every role
in the tenant can read.

* Contract versions come from the contract itself (``version:`` in the YAML) and
  must increase; the contract name must stay the same for an asset.
* Test-case and plan versions are numbered by the server per name (1, 2, 3...).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from rai.api.context import ROLES, TenantSession, require
from rai.api.listing import Page, PageOut, after_int, after_uuid, page, paged
from rai.api.problems import Problem
from rai.api.routes_projects import (
    check_environment,
    find_asset,
    find_project,
    is_unique_violation,
)
from rai.validation.contract import ContractError, parse_contract

router = APIRouter(prefix="/v1", tags=["definitions"])

anyone = require(*ROLES)
engineer = require("engineer")

MAX_SOURCE = 256 * 1024
MAX_DEFINITION = 64 * 1024
MAX_PLAN_ITEMS = 200
TestType = Literal[
    "contract_rule",
    "recovery_assertion",
    "consistency_check",
    "tevv_metric",
    "drift_check",
    "privacy_canary",
]
Severity = Literal["info", "warn", "critical"]
NAME = r"^[a-z][a-z0-9_.-]{0,99}$"


def _busy() -> Problem:
    return Problem(409, "definitions.concurrent_publish", "Another version was published; retry")


# ---- data contracts ------------------------------------------------------------------------


class ContractIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=MAX_SOURCE, description="Contract YAML or JSON")


class ContractSummary(BaseModel):
    contract_id: UUID
    asset_id: UUID
    name: str
    version: int
    ref: str
    spec_sha256: str
    created_by: UUID
    created_at: datetime


class ContractOut(ContractSummary):
    spec: dict[str, Any]
    source: str


_CONTRACT = (
    "SELECT contract_id, asset_id, name, version, name || '@' || version AS ref, spec_sha256, "
    "created_by, created_at{extra} FROM rai.data_contracts"
)


@router.get("/assets/{asset_id}/contracts")
def list_contracts(
    asset_id: UUID,
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
) -> PageOut[ContractSummary]:
    find_asset(session.conn, asset_id)
    after = after_int(wanted)
    rows = session.conn.execute(
        text(
            _CONTRACT.format(extra="")
            + " WHERE asset_id = :a AND version > :after ORDER BY version LIMIT :n"
        ),
        {"a": asset_id, "after": after, "n": wanted.limit + 1},
    ).all()
    items = [ContractSummary.model_validate(r._asdict()) for r in rows]
    return paged(items, wanted, lambda c: str(c.version))


def _find_contract(conn: Connection, asset_id: UUID, version: int) -> ContractOut:
    row = conn.execute(
        text(_CONTRACT.format(extra=", spec, source") + " WHERE asset_id = :a AND version = :v"),
        {"a": asset_id, "v": version},
    ).first()
    if row is None:
        raise Problem(404, "contracts.not_found", "Contract version not found")
    return ContractOut.model_validate(row._asdict())


@router.get("/assets/{asset_id}/contracts/{version}")
def get_contract(
    asset_id: UUID, version: int, session: Annotated[TenantSession, Depends(anyone)]
) -> ContractOut:
    return _find_contract(session.conn, asset_id, version)


@router.post("/assets/{asset_id}/contracts", status_code=201)
def publish_contract(
    asset_id: UUID,
    body: ContractIn,
    response: Response,
    session: Annotated[TenantSession, Depends(engineer)],
) -> ContractOut:
    """Publish a new contract version. It is checked exactly as ``rai contract check``
    checks it; problems come back as ``errors``."""
    asset = find_asset(session.conn, asset_id)
    find_project(session.conn, asset.project_id, for_change=True)
    try:
        contract = parse_contract(body.source)
    except ContractError as error:
        raise Problem(
            422, "contracts.invalid", "The contract is not valid",
            errors=[{"location": "body.source", "message": p} for p in error.problems],
        ) from None  # fmt: skip
    latest = session.conn.execute(
        text(
            "SELECT name, version FROM rai.data_contracts WHERE asset_id = :a "
            "ORDER BY version DESC LIMIT 1"
        ),
        {"a": asset_id},
    ).first()
    if latest is not None and latest.name != contract.contract:
        raise Problem(
            409, "contracts.name_mismatch", "The contract name differs from earlier versions",
            detail=f"this asset's contract is {latest.name!r}",
        )  # fmt: skip
    if latest is not None and contract.version <= latest.version:
        raise Problem(
            409, "contracts.version_not_newer", "Published versions never change",
            detail=f"the latest version is {latest.version}; publish {latest.version + 1} or later",
        )  # fmt: skip
    try:
        session.conn.execute(
            text(
                "INSERT INTO rai.data_contracts (asset_id, name, version, spec, spec_sha256, "
                "source, created_by) VALUES (:a, :n, :v, CAST(:spec AS jsonb), :sha, :src, :u)"
            ),
            {
                "a": asset_id, "n": contract.contract, "v": contract.version,
                "spec": contract.canonical_json(), "sha": contract.spec_sha256,
                "src": body.source, "u": session.user_id,
            },
        )  # fmt: skip
    except IntegrityError as error:
        if is_unique_violation(error):
            raise _busy() from None
        raise
    response.headers["Location"] = f"/v1/assets/{asset_id}/contracts/{contract.version}"
    return _find_contract(session.conn, asset_id, contract.version)


# ---- test cases -----------------------------------------------------------------------------


def _definition_size(value: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(value)) > MAX_DEFINITION:
        raise ValueError(f"definition must be at most {MAX_DEFINITION} bytes as JSON")
    return value


class TestCaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    type: TestType
    target_asset_id: UUID | None = None
    definition: dict[str, Any] = Field(default_factory=dict)
    severity: Severity = "critical"
    expected_result: str | None = Field(default=None, max_length=2000)

    _size = field_validator("definition")(_definition_size)


class TestCaseOut(BaseModel):
    test_id: UUID
    project_id: UUID
    name: str
    version: int
    type: TestType
    target_asset_id: UUID | None
    definition: dict[str, Any]
    severity: Severity
    expected_result: str | None
    created_by: UUID
    created_at: datetime


_TEST = (
    "SELECT test_id, project_id, name, version, type::text, target_asset_id, definition, "
    "severity::text, expected_result, created_by, created_at FROM rai.test_cases"
)


def _check_asset_in_project(conn: Connection, asset_id: UUID, project_id: UUID) -> None:
    found = conn.execute(
        text("SELECT 1 FROM rai.pipeline_assets WHERE asset_id = :a AND project_id = :p"),
        {"a": asset_id, "p": project_id},
    ).first()
    if found is None:
        raise Problem(422, "definitions.unknown_asset", "The asset is not in this project")


@router.get("/projects/{project_id}/test-cases")
def list_test_cases(
    project_id: UUID,
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
    name: Annotated[str | None, Query(max_length=100)] = None,
) -> PageOut[TestCaseOut]:
    find_project(session.conn, project_id)
    after = after_uuid(wanted)
    rows = session.conn.execute(
        text(
            f"{_TEST} WHERE project_id = :p AND (CAST(:name AS text) IS NULL OR name = :name) "
            "AND (CAST(:after AS uuid) IS NULL OR test_id > :after) ORDER BY test_id LIMIT :n"
        ),
        {"p": project_id, "name": name, "after": after, "n": wanted.limit + 1},
    ).all()
    items = [TestCaseOut.model_validate(r._asdict()) for r in rows]
    return paged(items, wanted, lambda t: str(t.test_id))


@router.post("/projects/{project_id}/test-cases", status_code=201)
def add_test_case(
    project_id: UUID,
    body: TestCaseIn,
    response: Response,
    session: Annotated[TenantSession, Depends(engineer)],
) -> TestCaseOut:
    """Add the next version of the test case called ``name`` (version 1 if new)."""
    find_project(session.conn, project_id, for_change=True)
    if body.type in ("contract_rule", "consistency_check") and body.target_asset_id is None:
        raise Problem(422, "test_cases.asset_required", f"A {body.type} test needs target_asset_id")
    if body.target_asset_id is not None:
        _check_asset_in_project(session.conn, body.target_asset_id, project_id)
    try:
        row = session.conn.execute(
            text(
                "INSERT INTO rai.test_cases (project_id, name, version, type, target_asset_id, "
                "definition, severity, expected_result, created_by) "
                "SELECT :p, :n, COALESCE(MAX(version), 0) + 1, CAST(:t AS rai.test_type), :a, "
                "CAST(:d AS jsonb), CAST(:s AS rai.severity), :e, :u "
                "FROM rai.test_cases WHERE project_id = :p AND name = :n RETURNING test_id"
            ),
            {
                "p": project_id, "n": body.name, "t": body.type, "a": body.target_asset_id,
                "d": json.dumps(body.definition), "s": body.severity, "e": body.expected_result,
                "u": session.user_id,
            },
        ).one()  # fmt: skip
    except IntegrityError as error:
        if is_unique_violation(error):
            raise _busy() from None
        raise
    response.headers["Location"] = f"/v1/projects/{project_id}/test-cases?name={body.name}"
    found = session.conn.execute(text(f"{_TEST} WHERE test_id = :t"), {"t": row.test_id}).one()
    return TestCaseOut.model_validate(found._asdict())


# ---- test plans -------------------------------------------------------------------------------


class PlanItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_type: Literal["test_case", "scenario"] = "test_case"
    test_id: UUID | None = None
    scenario_id: UUID | None = None
    contract_id: UUID | None = None


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    trigger: Literal["manual", "ci", "schedule"] = "manual"
    environment_id: UUID | None = Field(
        default=None, description="where its runs execute; default: the project's (B-22)"
    )
    items: list[PlanItemIn] = Field(min_length=1, max_length=MAX_PLAN_ITEMS)


class PlanItemOut(BaseModel):
    position: int
    item_type: Literal["test_case", "scenario"]
    test_id: UUID | None
    scenario_id: UUID | None
    contract_id: UUID | None


class PlanOut(BaseModel):
    plan_id: UUID
    project_id: UUID
    name: str
    version: int
    environment_id: UUID | None
    trigger: Literal["manual", "ci", "schedule"]
    created_by: UUID
    created_at: datetime
    items: list[PlanItemOut]


_PLAN = (
    "SELECT plan_id, project_id, name, version, environment_id, trigger::text, created_by, "
    "created_at FROM rai.test_plans"
)


def _plans(conn: Connection, rows: list[Any]) -> list[PlanOut]:
    ids = [r.plan_id for r in rows]
    items: dict[Any, list[PlanItemOut]] = {i: [] for i in ids}
    if ids:
        for item in conn.execute(
            text(
                "SELECT plan_id, position, item_type::text, test_id, scenario_id, contract_id "
                "FROM rai.plan_items WHERE plan_id = ANY(:ids) ORDER BY plan_id, position"
            ),
            {"ids": ids},
        ):
            data = item._asdict()
            items[data.pop("plan_id")].append(PlanItemOut.model_validate(data))
    return [PlanOut.model_validate(r._asdict() | {"items": items[r.plan_id]}) for r in rows]


def _check_items(conn: Connection, project_id: UUID, items: list[PlanItemIn]) -> None:
    for position, item in enumerate(items, start=1):
        where = f"items[{position - 1}]"
        if item.item_type == "scenario":  # B-26: a chaos scenario version
            if item.scenario_id is None or item.test_id is not None or item.contract_id:
                raise Problem(422, "plans.scenario_required",
                              f"{where}: a scenario item names scenario_id only")  # fmt: skip
            found = conn.execute(
                text(
                    "SELECT 1 FROM rai.chaos_scenarios WHERE scenario_id = :s AND project_id = :p"
                ),
                {"s": item.scenario_id, "p": project_id},
            ).first()
            if found is None:
                raise Problem(
                    422, "plans.unknown_scenario", f"{where}: the scenario is not in this project"
                )
            continue
        if item.scenario_id is not None:
            raise Problem(
                422, "plans.test_required", f"{where}: a test_case item has no scenario_id"
            )
        if item.test_id is None:
            raise Problem(422, "plans.test_required", f"{where}: test_id is required")
        test = conn.execute(
            text("SELECT type::text, target_asset_id FROM rai.test_cases "
                 "WHERE test_id = :t AND project_id = :p"),
            {"t": item.test_id, "p": project_id},
        ).first()  # fmt: skip
        if test is None:
            raise Problem(
                422, "plans.unknown_test", f"{where}: the test case is not in this project"
            )
        if item.contract_id is not None:
            contract = conn.execute(
                text("SELECT asset_id FROM rai.data_contracts WHERE contract_id = :c"),
                {"c": item.contract_id},
            ).first()
            if contract is None or contract.asset_id != test.target_asset_id:
                raise Problem(
                    422, "plans.contract_mismatch",
                    f"{where}: the contract must belong to the test case's target asset",
                )  # fmt: skip


@router.get("/projects/{project_id}/plans")
def list_plans(
    project_id: UUID,
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
    name: Annotated[str | None, Query(max_length=100)] = None,
) -> PageOut[PlanOut]:
    find_project(session.conn, project_id)
    after = after_uuid(wanted)
    rows = session.conn.execute(
        text(
            f"{_PLAN} WHERE project_id = :p AND (CAST(:name AS text) IS NULL OR name = :name) "
            "AND (CAST(:after AS uuid) IS NULL OR plan_id > :after) ORDER BY plan_id LIMIT :n"
        ),
        {"p": project_id, "name": name, "after": after, "n": wanted.limit + 1},
    ).all()
    return paged(_plans(session.conn, list(rows)), wanted, lambda p: str(p.plan_id))


@router.post("/projects/{project_id}/plans", status_code=201)
def create_plan(
    project_id: UUID,
    body: PlanIn,
    response: Response,
    session: Annotated[TenantSession, Depends(engineer)],
) -> PlanOut:
    """Create the next version of the plan called ``name``, with its items in order."""
    find_project(session.conn, project_id, for_change=True)
    _check_items(session.conn, project_id, body.items)
    if body.environment_id is not None:
        check_environment(session.conn, body.environment_id)
    try:
        plan_id = session.conn.execute(
            text(
                "INSERT INTO rai.test_plans (project_id, name, version, trigger, created_by, "
                "environment_id) SELECT :p, :n, COALESCE(MAX(version), 0) + 1, "
                "CAST(:t AS rai.plan_trigger), :u, :e "
                "FROM rai.test_plans WHERE project_id = :p AND name = :n RETURNING plan_id"
            ),
            {
                "p": project_id,
                "n": body.name,
                "t": body.trigger,
                "u": session.user_id,
                "e": body.environment_id,
            },
        ).scalar_one()
    except IntegrityError as error:
        if is_unique_violation(error):
            raise _busy() from None
        raise
    session.conn.execute(
        text(
            "INSERT INTO rai.plan_items (project_id, plan_id, position, item_type, test_id, "
            "scenario_id, contract_id) VALUES (:p, :plan, :pos, CAST(:k AS rai.plan_item_type), "
            ":t, :s, :c)"
        ),
        [
            {
                "p": project_id,
                "plan": plan_id,
                "pos": n,
                "k": i.item_type,
                "t": i.test_id,
                "s": i.scenario_id,
                "c": i.contract_id,
            }
            for n, i in enumerate(body.items, start=1)
        ],
    )
    response.headers["Location"] = f"/v1/projects/{project_id}/plans?name={body.name}"
    row = session.conn.execute(text(f"{_PLAN} WHERE plan_id = :p"), {"p": plan_id}).one()
    return _plans(session.conn, [row])[0]
