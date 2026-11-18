"""Projects, assets, contracts, test cases and plans through the API (B-17).

Against a real PostgreSQL 16. Covers: roles per endpoint, tenant isolation (404),
immutable versions, version numbering, ETag / If-Match, pagination, audit entries,
and the database refusing what the API would never send.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from rai.core.tenancy import set_context
from rai.validation.contract import load_contract
from tests.integration.apikit import (
    Tenant,
    auth,
    login,
    needs_db,
    new_tenant,
    signed_in_member,
)

pytestmark = needs_db

ROOT = Path(__file__).resolve().parents[2]
TXN_YAML = (ROOT / "contracts" / "sandboxbank" / "transactions_raw.yaml").read_text("utf-8")


class World:
    """One tenant with an admin, an engineer and a viewer, signed in."""

    def __init__(self, client: TestClient, owner: Engine) -> None:
        self.client = client
        self.tenant: Tenant = new_tenant(owner)
        self.admin = auth(login(client, self.tenant.admin_email), self.tenant.tenant_id)
        self.engineer = signed_in_member(client, owner, self.tenant, "engineer")
        self.viewer = signed_in_member(client, owner, self.tenant, "viewer")

    def project(self, name: str | None = None) -> dict[str, Any]:
        body = {"name": name or f"proj-{uuid4().hex[:8]}"}
        response = self.client.post("/v1/projects", json=body, headers=self.engineer)
        assert response.status_code == 201, response.text
        project: dict[str, Any] = response.json()
        return project

    def asset(self, project_id: str, name: str = "transactions_raw") -> dict[str, Any]:
        body = {"kind": "table", "name": name, "locator": "public.transactions_raw"}
        response = self.client.post(
            f"/v1/projects/{project_id}/assets", json=body, headers=self.engineer
        )
        assert response.status_code == 201, response.text
        asset: dict[str, Any] = response.json()
        return asset


@pytest.fixture(scope="module")
def world(client: TestClient, owner: Engine) -> World:
    return World(client, owner)


def bump(source: str, version: int) -> str:
    return source.replace("version: 1\n", f"version: {version}\n", 1)


# ---- projects ----------------------------------------------------------------------------------


def test_projects_create_read_and_roles(world: World) -> None:
    project = world.project("payments")
    assert project["owner_id"]
    assert (project["data_classification"], project["status"], project["revision"]) == (
        "synthetic", "active", 1,
    )  # fmt: skip
    got = world.client.get(f"/v1/projects/{project['project_id']}", headers=world.viewer)
    assert (got.status_code, got.headers["etag"]) == (200, '"v1"')
    assert (
        world.client.post("/v1/projects", json={"name": "x"}, headers=world.admin).status_code
        == 201
    )
    denied = world.client.post("/v1/projects", json={"name": "y"}, headers=world.viewer)
    assert (denied.status_code, denied.json()["code"]) == (403, "auth.forbidden")
    taken = world.client.post("/v1/projects", json={"name": "payments"}, headers=world.engineer)
    assert (taken.status_code, taken.json()["code"]) == (409, "projects.name_taken")
    stranger = world.client.post(
        "/v1/projects", json={"name": "z", "owner_id": str(uuid4())}, headers=world.engineer
    )
    assert stranger.json()["code"] == "projects.owner_not_member"


def test_project_updates_need_the_current_etag(world: World) -> None:
    project = world.project()
    url = f"/v1/projects/{project['project_id']}"
    change = {"description": "Nightly settlement", "status": "archived"}
    assert world.client.patch(url, json=change, headers=world.engineer).status_code == 428
    stale = world.client.patch(url, json=change, headers=world.engineer | {"If-Match": '"v9"'})
    assert (stale.status_code, stale.json()["code"]) == (412, "request.stale")
    garbled = world.client.patch(url, json=change, headers=world.engineer | {"If-Match": "abc"})
    assert garbled.status_code == 412
    done = world.client.patch(url, json=change, headers=world.engineer | {"If-Match": '"v1"'})
    assert (done.status_code, done.headers["etag"], done.json()["status"]) == (
        200,
        '"v2"',
        "archived",
    )
    again = world.client.patch(url, json=change, headers=world.engineer | {"If-Match": '"v1"'})
    assert again.status_code == 412  # someone else's change wins only once
    cleared = world.client.patch(
        url, json={"name": None}, headers=world.engineer | {"If-Match": '"v2"'}
    )
    assert cleared.status_code == 422
    nothing = world.client.patch(url, json={}, headers=world.engineer | {"If-Match": '"v2"'})
    assert nothing.json()["revision"] == 2
    archived = world.client.post(
        f"{url}/assets", json={"kind": "table", "name": "late"}, headers=world.engineer
    )
    assert (archived.status_code, archived.json()["code"]) == (409, "projects.archived")
    missing = world.client.patch(
        f"/v1/projects/{uuid4()}", json=change, headers=world.engineer | {"If-Match": '"v1"'}
    )
    assert missing.status_code == 404
    renamed = world.client.patch(
        url, json={"name": "payments"}, headers=world.engineer | {"If-Match": '"v2"'}
    )
    assert renamed.json()["code"] == "projects.name_taken"


def test_lists_are_paginated(world: World) -> None:
    project = world.project()
    for n in range(5):
        world.asset(project["project_id"], f"asset_{n}")
    url = f"/v1/projects/{project['project_id']}/assets"
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        body = world.client.get(url, params=params, headers=world.viewer).json()
        seen += [a["name"] for a in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"asset_{n}" for n in range(5)]  # UUIDv7: creation order
    bad = world.client.get(url, params={"cursor": "!!"}, headers=world.viewer)
    assert (bad.status_code, bad.json()["code"]) == (400, "request.invalid_cursor")
    too_many = world.client.get(url, params={"limit": 500}, headers=world.viewer)
    assert too_many.status_code == 422
    listed = world.client.get("/v1/projects", params={"status": "active"}, headers=world.viewer)
    assert project["project_id"] in {p["project_id"] for p in listed.json()["items"]}


# ---- assets and contracts ----------------------------------------------------------------------


def test_contracts_are_versioned_and_immutable(world: World) -> None:
    project = world.project()
    asset = world.asset(project["project_id"])
    url = f"/v1/assets/{asset['asset_id']}/contracts"
    first = world.client.post(url, json={"source": TXN_YAML}, headers=world.engineer)
    assert first.status_code == 201, first.text
    published = first.json()
    expected = load_contract(ROOT / "contracts" / "sandboxbank" / "transactions_raw.yaml")
    assert (published["ref"], published["spec_sha256"]) == (expected.ref, expected.spec_sha256)
    assert published["source"] == TXN_YAML
    assert first.headers["location"] == f"{url}/1"

    same = world.client.post(url, json={"source": TXN_YAML}, headers=world.engineer)
    assert (same.status_code, same.json()["code"]) == (409, "contracts.version_not_newer")
    renamed = TXN_YAML.replace("contract: transactions_raw", "contract: payments", 1)
    other = world.client.post(url, json={"source": bump(renamed, 2)}, headers=world.engineer)
    assert other.json()["code"] == "contracts.name_mismatch"
    second = world.client.post(url, json={"source": bump(TXN_YAML, 3)}, headers=world.engineer)
    assert second.json()["ref"] == "transactions_raw@3"

    broken = world.client.post(
        url, json={"source": "contract: x\nversion: 9\ncolumns: {a: {type: nope}}\n"},
        headers=world.engineer,
    )  # fmt: skip
    assert (broken.status_code, broken.json()["code"]) == (422, "contracts.invalid")
    assert "unknown type 'nope'" in broken.json()["errors"][0]["message"]

    versions = world.client.get(url, params={"limit": 1}, headers=world.viewer).json()
    assert [c["version"] for c in versions["items"]] == [1]
    rest = world.client.get(url, params={"cursor": versions["next_cursor"]}, headers=world.viewer)
    assert [c["version"] for c in rest.json()["items"]] == [3]
    assert world.client.get(f"{url}/2", headers=world.viewer).status_code == 404
    assert world.client.get(f"{url}/3", headers=world.viewer).json()["spec"]["version"] == 3
    denied = world.client.post(url, json={"source": bump(TXN_YAML, 4)}, headers=world.admin)
    assert denied.status_code == 403  # catalogue: engineers publish contracts
    assert (
        world.client.get(f"/v1/assets/{uuid4()}/contracts", headers=world.viewer).status_code == 404
    )


def test_assets_have_unique_names_per_project(world: World) -> None:
    project = world.project()
    asset = world.asset(project["project_id"], "ledger_entries")
    assert world.client.get(f"/v1/assets/{asset['asset_id']}", headers=world.viewer).json() == asset
    again = world.client.post(
        f"/v1/projects/{project['project_id']}/assets",
        json={"kind": "table", "name": "ledger_entries"}, headers=world.engineer,
    )  # fmt: skip
    assert (again.status_code, again.json()["code"]) == (409, "assets.name_taken")
    bad = world.client.post(
        f"/v1/projects/{project['project_id']}/assets",
        json={"kind": "table", "name": "Ledger Entries"}, headers=world.engineer,
    )  # fmt: skip
    assert bad.status_code == 422


# ---- test cases and plans ----------------------------------------------------------------------


def _test_case(world: World, project_id: str, **body: Any) -> dict[str, Any]:
    response = world.client.post(
        f"/v1/projects/{project_id}/test-cases", json=body, headers=world.engineer
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def test_test_cases_get_the_next_version_per_name(world: World) -> None:
    project = world.project()
    pid = project["project_id"]
    asset = world.asset(pid)
    target = {"type": "contract_rule", "target_asset_id": asset["asset_id"]}
    first = _test_case(
        world, pid, name="txn.contract", definition={"fail_on": "critical"}, **target
    )
    second = _test_case(world, pid, name="txn.contract", severity="warn", **target)
    other = _test_case(world, pid, name="ledger.consistency", type="consistency_check")
    assert (first["version"], second["version"], other["version"]) == (1, 2, 1)
    assert first["definition"] == {"fail_on": "critical"}
    listed = world.client.get(
        f"/v1/projects/{pid}/test-cases", params={"name": "txn.contract"}, headers=world.viewer
    ).json()
    assert [t["version"] for t in listed["items"]] == [1, 2]
    no_asset = world.client.post(
        f"/v1/projects/{pid}/test-cases", json={"name": "x", "type": "contract_rule"},
        headers=world.engineer,
    )  # fmt: skip
    assert no_asset.json()["code"] == "test_cases.asset_required"
    elsewhere = world.asset(world.project()["project_id"])
    foreign = world.client.post(
        f"/v1/projects/{pid}/test-cases",
        json={"name": "x", "type": "contract_rule", "target_asset_id": elsewhere["asset_id"]},
        headers=world.engineer,
    )
    assert foreign.json()["code"] == "definitions.unknown_asset"
    huge = world.client.post(
        f"/v1/projects/{pid}/test-cases",
        json={"name": "x", "type": "drift_check", "definition": {"k": "v" * 70_000}},
        headers=world.engineer,
    )
    assert huge.status_code == 422


def test_plans_pin_test_cases_and_contracts(world: World) -> None:
    pid = world.project()["project_id"]
    asset = world.asset(pid)
    contract = world.client.post(
        f"/v1/assets/{asset['asset_id']}/contracts",
        json={"source": TXN_YAML},
        headers=world.engineer,
    ).json()
    test = _test_case(world, pid, name="txn.contract", type="contract_rule",
                      target_asset_id=asset["asset_id"])  # fmt: skip
    check = _test_case(world, pid, name="ledger.consistency", type="consistency_check")
    items = [
        {"test_id": test["test_id"], "contract_id": contract["contract_id"]},
        {"test_id": check["test_id"]},
    ]
    url = f"/v1/projects/{pid}/plans"
    made = world.client.post(url, json={"name": "ci-contracts", "trigger": "ci", "items": items},
                             headers=world.engineer)  # fmt: skip
    assert made.status_code == 201, made.text
    plan = made.json()
    assert (plan["version"], plan["environment_id"]) == (1, None)
    assert [(i["position"], i["test_id"]) for i in plan["items"]] == [
        (1, test["test_id"]), (2, check["test_id"]),
    ]  # fmt: skip
    again = world.client.post(url, json={"name": "ci-contracts", "items": items[:1]},
                              headers=world.engineer).json()  # fmt: skip
    assert again["version"] == 2
    listed = world.client.get(url, params={"name": "ci-contracts"}, headers=world.viewer).json()
    assert [(p["version"], len(p["items"])) for p in listed["items"]] == [(1, 2), (2, 1)]

    def refused(item: dict[str, Any]) -> str:
        response = world.client.post(
            url, json={"name": "bad", "items": [item]}, headers=world.engineer
        )
        assert response.status_code == 422, response.text
        code: str = response.json()["code"]
        return code

    unknown = {"item_type": "scenario", "scenario_id": str(uuid4())}
    assert refused(unknown) == "plans.unknown_scenario"  # B-26: scenarios exist now
    assert refused({}) == "plans.test_required"
    assert refused({"test_id": str(uuid4())}) == "plans.unknown_test"
    assert refused({"test_id": check["test_id"], "contract_id": contract["contract_id"]}) == (
        "plans.contract_mismatch"
    )
    empty = world.client.post(url, json={"name": "bad", "items": []}, headers=world.engineer)
    assert empty.status_code == 422


# ---- isolation, immutability and audit in the database -----------------------------------------


def test_another_tenant_sees_none_of_it(world: World, client: TestClient, owner: Engine) -> None:
    pid = world.project()["project_id"]
    asset = world.asset(pid)
    other = new_tenant(owner)
    outsider = auth(login(client, other.admin_email), other.tenant_id)
    aid = asset["asset_id"]
    urls = [f"/v1/projects/{pid}", f"/v1/projects/{pid}/assets", f"/v1/assets/{aid}",
            f"/v1/assets/{aid}/contracts", f"/v1/projects/{pid}/test-cases",
            f"/v1/projects/{pid}/plans"]  # fmt: skip
    for url in urls:
        assert client.get(url, headers=outsider).status_code == 404, url
    assert pid not in {
        p["project_id"] for p in client.get("/v1/projects", headers=outsider).json()["items"]
    }


def test_definitions_cannot_change_or_cross_tenants(
    world: World, owner: Engine, api_url: str
) -> None:
    pid = world.project()["project_id"]
    world.asset(pid)
    _test_case(world, pid, name="t", type="drift_check")
    other = new_tenant(owner)
    engine = create_engine(api_url)
    try:
        with engine.begin() as conn:
            set_context(conn, world.tenant.tenant_id, "user", "test")
            for statement in (
                "UPDATE rai.test_cases SET severity = 'info'",
                "DELETE FROM rai.test_cases",
                "DELETE FROM rai.pipeline_assets",
            ):
                with pytest.raises(DBAPIError), conn.begin_nested():
                    conn.execute(text(statement))
        with engine.begin() as conn:  # a row of tenant B pointing at tenant A's project
            set_context(conn, other.tenant_id, "user", "test")
            with pytest.raises(IntegrityError):
                conn.execute(
                    text("INSERT INTO rai.pipeline_assets (project_id, kind, name, created_by) "
                         "VALUES (:p, 'table', 'sneaky', :u)"),
                    {"p": UUID(pid), "u": other.admin_id},
                )  # fmt: skip
    finally:
        engine.dispose()
    with owner.connect() as conn:
        actions = {
            row.action
            for row in conn.execute(
                text("SELECT action FROM rai.audit_chain WHERE tenant_id = :t"),
                {"t": world.tenant.tenant_id},
            )
        }
    assert {"project.insert", "asset.insert", "test_case.insert"} <= actions
