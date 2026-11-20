"""Environments and the target allow-list against PostgreSQL 16 (B-22: NFR-SAF-1, FR-07.2).

Acceptance (spec NFR-SAF-1, Data Model section 5): production is refused at the
schema level. ``env_type`` has no PRODUCTION value and ``production_flag`` can
only be false, so no API bug can register a production environment; tests run
only in registered, active environments (deny by default).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from rai.worker.service import Worker
from tests.integration.apikit import needs_db, new_tenant, signed_in_member
from tests.integration.runkit import Lab, config, drain
from tests.unit.test_chaos_agent import IMAGE

pytestmark = needs_db


def code(response: Any) -> tuple[int, str]:
    return response.status_code, response.json().get("code", "")


def environment(lab: Lab, name: str, kind: str = "STAGING", **extra: Any) -> dict[str, Any]:
    body = {"name": name, "type": kind} | extra
    created: dict[str, Any] = lab.post("/v1/environments", body, lab.admin)
    return created


def owner_insert(owner: Engine, lab: Lab, sql: str) -> None:
    """Straight into the table as the schema owner: no API in the way."""
    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rai.environments (tenant_id, name, type, production_flag, created_by) "
                f"SELECT CAST(:t AS uuid), {sql}, CAST(:u AS uuid)"
            ),
            {"t": str(lab.tenant.tenant_id), "u": str(lab.tenant.admin_id)},
        )


# ---- acceptance: production cannot be represented --------------------------------------------


def test_production_is_refused_at_the_schema_level(lab: Lab, owner: Engine) -> None:
    """NFR-SAF-1: neither the API nor the database can hold a production environment."""
    refused = lab.client.post(
        "/v1/environments", json={"name": "LEDGER-01", "type": "PRODUCTION"}, headers=lab.admin
    )
    assert refused.status_code == 422
    named = lab.client.post(
        "/v1/environments", json={"name": "PROD-COPY", "type": "SANDBOX"}, headers=lab.admin
    )
    assert named.status_code == 422

    with pytest.raises(DBAPIError, match=r"invalid input value for enum rai\.env_type"):
        owner_insert(owner, lab, "'LEDGER-01', 'PRODUCTION', false")
    with pytest.raises(DBAPIError, match="environments_production_flag_check"):
        owner_insert(owner, lab, "'LEDGER-02', 'SANDBOX', true")
    with pytest.raises(DBAPIError, match="environments_name_check"):
        owner_insert(owner, lab, "'PROD-01', 'SANDBOX', false")
    with owner.connect() as conn:
        values = conn.execute(text("SELECT enum_range(NULL::rai.env_type)::text")).scalar_one()
    assert values == "{SANDBOX,STAGING}"


def test_an_environment_never_changes_what_it_is(lab: Lab, owner: Engine) -> None:
    staging = environment(lab, "STAGING-01")
    with owner.begin() as conn, pytest.raises(DBAPIError, match="name and type never change"):
        conn.execute(
            text("UPDATE rai.environments SET type = 'SANDBOX' WHERE environment_id = :e"),
            {"e": staging["environment_id"]},
        )
    with owner.begin() as conn, pytest.raises(DBAPIError, match="insert-only"):
        conn.execute(
            text("DELETE FROM rai.environments WHERE environment_id = :e"),
            {"e": staging["environment_id"]},
        )


# ---- the API ---------------------------------------------------------------------------------


def test_registering_reading_and_updating(lab: Lab, client: TestClient, owner: Engine) -> None:
    staging = environment(lab, "STAGING-01", max_fault_duration_seconds=600)
    assert (staging["type"], staging["production_flag"], staging["status"]) == (
        "STAGING", False, "active",
    )  # fmt: skip
    assert staging["max_fault_duration_seconds"] == 600
    assert environment(lab, "SANDBOX-02", "SANDBOX")["max_fault_duration_seconds"] == 900
    path = f"/v1/environments/{staging['environment_id']}"

    duplicate = client.post("/v1/environments", json={"name": "STAGING-01", "type": "STAGING"},
                            headers=lab.admin)  # fmt: skip
    assert code(duplicate) == (409, "environments.name_taken")
    too_long = client.post("/v1/environments", json={"name": "STAGING-02", "type": "STAGING",
                           "max_fault_duration_seconds": 3601}, headers=lab.admin)  # fmt: skip
    assert too_long.status_code == 422
    assert client.post("/v1/environments", json={"name": "X-1", "type": "SANDBOX"},
                       headers=lab.engineer).status_code == 403  # fmt: skip
    listed = client.get("/v1/environments", headers=lab.viewer).json()["items"]
    assert {e["name"] for e in listed} == {"SANDBOX-01", "SANDBOX-02", "STAGING-01"}

    read = client.get(path, headers=lab.viewer)
    assert read.headers["ETag"] == '"v1"'
    assert client.patch(path, json={"status": "retired"}, headers=lab.admin).status_code == 428
    assert code(client.patch(path, json={"type": "SANDBOX"},
                             headers=lab.admin | {"If-Match": '"v1"'}))[0] == 422  # fmt: skip
    changed = client.patch(path, json={"max_fault_duration_seconds": 300},
                           headers=lab.admin | {"If-Match": '"v1"'})  # fmt: skip
    assert changed.status_code == 200, changed.text
    assert (changed.json()["max_fault_duration_seconds"], changed.headers["ETag"]) == (300, '"v2"')
    stale = client.patch(path, json={"status": "retired"}, headers=lab.admin | {"If-Match": '"v1"'})
    assert code(stale) == (412, "request.stale")
    assert client.patch(path, json={"status": "retired"},
                        headers=lab.engineer | {"If-Match": '"v2"'}).status_code == 403  # fmt: skip

    stranger = signed_in_member(client, owner, new_tenant(owner), "admin")
    assert client.get(path, headers=stranger).status_code == 404
    assert client.get("/v1/environments", headers=stranger).json()["items"] == []


def test_the_target_allow_list(lab: Lab, client: TestClient, owner: Engine) -> None:
    staging = environment(lab, "STAGING-01")
    targets = f"/v1/environments/{staging['environment_id']}/targets"
    proxy = {"kind": "proxy", "name": "ledger-db-proxy", "required_label": "rai.target=ledger-db",
             "health_url": "http://127.0.0.1:8474/version"}  # fmt: skip

    unlabelled = client.post(targets, json=proxy | {"required_label": None}, headers=lab.admin)
    assert code(unlabelled) == (422, "targets.label_required")
    added = client.post(targets, json=proxy, headers=lab.admin)
    assert added.status_code == 201, added.text
    assert code(client.post(targets, json=proxy, headers=lab.admin)) == (409, "targets.name_taken")
    db = client.post(targets, json={"kind": "db", "name": "ledger-db"}, headers=lab.admin)
    assert db.status_code == 201
    assert client.post(targets, json={"kind": "http", "name": "api", "health_url": "ftp://x"},
                       headers=lab.admin).status_code == 422  # fmt: skip
    assert client.post(targets, json={"kind": "db", "name": "x"},
                       headers=lab.engineer).status_code == 403  # fmt: skip
    listed = client.get(targets, headers=lab.viewer).json()["items"]
    assert [t["name"] for t in listed] == ["ledger-db-proxy", "ledger-db"]

    # B-45: a container target names the image it runs; nothing else has one.
    container = {"kind": "container", "name": "payments-api",
                 "required_label": "rai.target=payments-api"}  # fmt: skip
    assert code(client.post(targets, json=container, headers=lab.admin)) == (
        422, "targets.image_required")  # fmt: skip
    assert client.post(targets, json=container | {"image_digest": "payments:latest"},
                       headers=lab.admin).status_code == 422  # fmt: skip
    assert code(client.post(targets, json=proxy | {"name": "p2", "image_digest": IMAGE},
                            headers=lab.admin)) == (422, "targets.image_not_container")  # fmt: skip
    pinned = client.post(targets, json=container | {"image_digest": IMAGE}, headers=lab.admin)
    assert pinned.status_code == 201, pinned.text
    assert pinned.json()["image_digest"] == IMAGE
    assert client.delete(f"{targets}/{pinned.json()['target_id']}",
                         headers=lab.admin).status_code == 204  # fmt: skip

    removed = client.delete(f"{targets}/{db.json()['target_id']}", headers=lab.admin)
    assert removed.status_code == 204
    assert (
        client.delete(f"{targets}/{db.json()['target_id']}", headers=lab.admin).status_code == 404
    )

    path = f"/v1/environments/{staging['environment_id']}"
    retired = client.patch(
        path, json={"status": "retired"}, headers=lab.admin | {"If-Match": '"v1"'}
    )
    assert retired.json()["status"] == "retired"
    late = client.post(targets, json={"kind": "db", "name": "later"}, headers=lab.admin)
    assert code(late) == (409, "environments.retired")
    with owner.begin() as conn, pytest.raises(DBAPIError, match="is retired"):
        conn.execute(
            text(
                "INSERT INTO rai.environment_targets (tenant_id, environment_id, kind, name, "
                "created_by) SELECT tenant_id, environment_id, 'db', 'sneaky', created_by "
                "FROM rai.environments WHERE environment_id = :e"
            ),
            {"e": staging["environment_id"]},
        )
    with owner.connect() as conn:
        actions = conn.execute(
            text(
                "SELECT action FROM rai.audit_chain WHERE subject_type = 'environment_target' "
                "AND subject_id = :t ORDER BY seq"
            ),
            {"t": db.json()["target_id"]},
        ).scalars().all()  # fmt: skip
    assert actions == ["environment_target.insert", "environment_target.delete"]


# ---- runs: pinned environment, deny by default -----------------------------------------------


def test_projects_and_plans_choose_where_runs_execute(lab: Lab, client: TestClient) -> None:
    staging = environment(lab, "STAGING-01")["environment_id"]
    project = client.get(f"/v1/projects/{lab.project}", headers=lab.engineer)
    current = {"If-Match": project.headers["ETag"]}
    patched = client.patch(f"/v1/projects/{lab.project}",
                           json={"default_environment_id": lab.environment},
                           headers=lab.engineer | current)  # fmt: skip
    assert patched.json()["default_environment_id"] == lab.environment

    by_default = lab.start(lab.plan(lab.tests["batch_0"]))
    assert by_default["environment_id"] == lab.environment
    pins = lab.run(by_default["run_id"])["pins"]["environment"]
    assert (pins["name"], pins["type"]) == ("SANDBOX-01", "SANDBOX")

    body = {"name": f"p-{uuid4().hex[:6]}", "environment_id": staging,
            "items": [{"test_id": lab.tests["batch_0"]}]}  # fmt: skip
    plan = lab.post(f"/v1/projects/{lab.project}/plans", body)
    assert plan["environment_id"] == staging
    assert lab.start(plan["plan_id"])["environment_id"] == staging

    nowhere = body | {"environment_id": str(uuid4())}
    unknown = client.post(f"/v1/projects/{lab.project}/plans", json=nowhere, headers=lab.engineer)
    assert code(unknown) == (422, "environments.not_usable")

    path = f"/v1/environments/{staging}"
    client.patch(path, json={"status": "retired"}, headers=lab.admin | {"If-Match": '"v1"'})
    refused = client.post("/v1/assurance/runs", json={"plan_id": plan["plan_id"]},
                          headers=lab.engineer)  # fmt: skip
    assert code(refused) == (409, "runs.environment_retired")


def test_the_worker_runs_tests_only_in_registered_environments(
    lab: Lab, client: TestClient, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    """Deny by default: unregistered, retired or not the run's environment -> refused."""
    asset = lab.assets["batch_0"]
    unknown = lab.start(lab.plan(lab.test_case("unknown", asset, environment="SANDBOX-99")))
    staging = environment(lab, "STAGING-01")
    elsewhere = {"name": f"p-{uuid4().hex[:6]}", "environment_id": staging["environment_id"],
                 "items": [{"test_id": lab.tests["batch_0"]}]}  # fmt: skip
    mismatch = lab.start(lab.post(f"/v1/projects/{lab.project}/plans", elsewhere)["plan_id"])
    fine = lab.start(lab.plan(lab.tests["batch_0"]))
    sandbox = client.get(f"/v1/environments/{lab.environment}", headers=lab.admin)
    retired_later = lab.start(lab.plan(lab.tests["batch_0"]))
    drain(Worker(worker_engine, config(feed, tmp_path)))
    assert lab.run(fine["run_id"])["state"] == "passed"

    for run, reason in (
        (unknown, "environment 'SANDBOX-99' is not registered"),
        (mismatch, "environment 'SANDBOX-01' is not the run's environment 'STAGING-01'"),
    ):
        refused = lab.run(run["run_id"])
        assert refused["state"] == "refused"
        assert reason in refused["reason"]

    client.patch(f"/v1/environments/{lab.environment}", json={"status": "retired"},
                 headers=lab.admin | {"If-Match": sandbox.headers["ETag"]})  # fmt: skip
    queued = lab.start(lab.plan(lab.tests["batch_0"]))  # the project has no default: not pinned
    drain(Worker(worker_engine, config(feed, tmp_path)))
    assert "environment 'SANDBOX-01' is retired" in lab.run(queued["run_id"])["reason"]
    assert lab.run(retired_later["run_id"])["state"] == "passed"  # ran before the retirement
