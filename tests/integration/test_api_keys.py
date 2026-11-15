"""Project API keys and the CI gate against PostgreSQL 16 (B-20: FR-06.1; threats S-02, D-03).

Acceptance (spec FR-06.1): ``rai run`` returns a non-zero exit code on gate
failure; a failing contract blocks the pipeline. The GitHub Action wraps the same
command (``action.yml``); CI runs it against a live API (job "CI gate").
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from rai.api.app import create_app
from rai.api.settings import Settings
from rai.cli.run import Gate, Reply
from rai.core import apikeys
from rai.worker.service import Worker
from tests.integration.apikit import SECRET, auth, login, needs_db, new_tenant, signed_in_member
from tests.integration.runkit import Lab, config, drain

pytestmark = needs_db


class ClientHttp:
    """``rai run``'s HTTP interface over the test client."""

    def __init__(self, client: TestClient, key: str) -> None:
        self.client, self.key = client, key

    def request(self, method: str, path: str, *, body: Any = None,
                headers: dict[str, str] | None = None) -> Reply:  # fmt: skip
        response = self.client.request(
            method,
            path,
            json=body,
            headers=bearer(self.key) | (headers or {}),
        )
        return Reply(response.status_code, response.json() if response.content else None)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def new_key(lab: Lab, project: str | None = None, **body: Any) -> dict[str, Any]:
    created: dict[str, Any] = lab.post(
        "/v1/api-keys", {"project_id": project or lab.project, "name": "github-actions"} | body
    )
    return created


def named_plan(lab: Lab, name: str, *test_ids: str) -> dict[str, Any]:
    body = {"name": name, "items": [{"test_id": t} for t in test_ids]}
    plan: dict[str, Any] = lab.post(f"/v1/projects/{lab.project}/plans", body)
    return plan


# ---- acceptance ------------------------------------------------------------------------------


def test_a_failing_contract_fails_the_ci_gate(
    lab: Lab, owner: Engine, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    """FR-06.1 acceptance: exit 0 on a clean batch, exit 1 when a contract fails."""
    key = new_key(lab)["key"]
    worker = Worker(worker_engine, config(feed, tmp_path))
    lines: list[str] = []
    gate = Gate(ClientHttp(lab.client, key), lines.append, sleep=lambda _: drain(worker))

    named_plan(lab, "ci-contracts", lab.tests["batch_0"])
    clean = gate.run("ci-contracts", trigger_ref="4f2a9c1", interval=0)
    assert clean.code == 0, lines
    assert lines[-1].startswith("PASSED run ")

    # The pipeline changes: the plan's newest version also checks the next batch,
    # which re-sends rows from the first.
    named_plan(lab, "ci-contracts", lab.tests["batch_0"], lab.tests["batch_1"])
    blocked = gate.run("ci-contracts", trigger_ref="9b3e0aa", interval=0)
    assert blocked.code == 1, lines
    assert [o["rule"] for o in blocked.failing] == ["unique_across_batches:txn_id"]
    assert any("FAIL  item-2 unique_across_batches:txn_id (critical)" in line for line in lines)

    run = blocked.run
    assert run is not None
    me = lab.client.get("/v1/me", headers=lab.engineer).json()
    assert (run["trigger_ref"], run["triggered_by"]) == ("9b3e0aa", me["user_id"])
    assert run["api_key_id"] is not None
    with owner.connect() as conn:
        actor = conn.execute(
            text(
                "SELECT actor_type::text, actor_id FROM rai.audit_chain "
                "WHERE action = 'run.insert' AND subject_id = :r"
            ),
            {"r": run["run_id"]},
        ).one()
    assert tuple(actor) == ("api_key", run["api_key_id"])


# ---- managing keys ---------------------------------------------------------------------------


def test_creating_listing_and_revoking_keys(lab: Lab, client: TestClient, owner: Engine) -> None:
    created = new_key(lab)
    assert created["scopes"] == ["runs:read", "runs:start"]
    assert created["state"] == "active"
    assert created["key"].startswith(f"rai_{created['prefix']}_")
    lifetime = datetime.fromisoformat(created["expires_at"]) - datetime.fromisoformat(
        created["created_at"]
    )
    assert lifetime == timedelta(days=30)

    listed = client.get("/v1/api-keys", params={"project_id": lab.project}, headers=lab.engineer)
    assert [k["api_key_id"] for k in listed.json()["items"]] == [created["api_key_id"]]
    assert "key" not in listed.json()["items"][0]
    assert created["key"] not in listed.text
    with owner.connect() as conn:
        stored = conn.execute(
            text("SELECT key_hash FROM rai.api_keys WHERE api_key_id = :k"),
            {"k": created["api_key_id"]},
        ).scalar_one()
    assert stored == apikeys.hash_secret(created["key"].split("_", 2)[2])

    def refused(body: dict[str, Any], headers: dict[str, str]) -> Any:
        return client.post("/v1/api-keys", json={"project_id": lab.project, "name": "k"} | body,
                           headers=headers)  # fmt: skip

    assert refused({"expires_in_days": 91}, lab.engineer).status_code == 422
    assert refused({"scopes": []}, lab.engineer).status_code == 422
    assert refused({"scopes": ["members:write"]}, lab.engineer).status_code == 422
    assert refused({}, lab.viewer).status_code == 403
    stranger = signed_in_member(client, owner, new_tenant(owner), "engineer")
    assert refused({}, stranger).status_code == 404  # another tenant's project
    path = f"/v1/api-keys/{created['api_key_id']}"
    assert client.get(path, headers=stranger).status_code == 404

    assert client.get("/v1/assurance/runs", headers=bearer(created["key"])).status_code == 200
    assert client.delete(path, headers=lab.engineer).status_code == 204
    assert client.delete(path, headers=lab.engineer).status_code == 204  # already revoked
    assert client.get(path, headers=lab.engineer).json()["state"] == "revoked"
    gone = client.get("/v1/assurance/runs", headers=bearer(created["key"]))
    assert (gone.status_code, gone.json()["code"]) == (401, "auth.invalid_key")
    with owner.connect() as conn:
        actions = conn.execute(
            text("SELECT action FROM rai.audit_chain WHERE subject_id = :k ORDER BY seq"),
            {"k": created["api_key_id"]},
        ).scalars().all()  # fmt: skip
    assert actions == ["api_key.insert", "api_key.update"]


# ---- what a key may do -----------------------------------------------------------------------


def test_a_key_works_only_within_its_project_and_scopes(
    lab: Lab, client: TestClient, owner: Engine
) -> None:
    key = bearer(new_key(lab)["key"])
    reader = bearer(new_key(lab, scopes=["runs:read"])["key"])
    named_plan(lab, "ci-contracts", lab.tests["batch_0"])
    other_project = lab.post("/v1/projects", {"name": f"q-{uuid4().hex[:6]}"})["project_id"]
    elsewhere = lab.start(lab.plan(lab.tests["batch_0"]))  # a user's run in the first project
    stranger_tenant = new_tenant(owner)

    started = client.post("/v1/assurance/runs", json={"plan": "ci-contracts"}, headers=key)
    assert started.status_code == 202, started.text
    assert client.get(f"/v1/assurance/runs/{elsewhere['run_id']}", headers=key).status_code == 200

    def code(response: Any) -> tuple[int, str]:
        return response.status_code, response.json()["code"]

    run_body = {"plan": "ci-contracts"}
    assert code(client.post("/v1/assurance/runs", json=run_body, headers=reader)) == (
        403, "auth.key_scope",
    )  # fmt: skip
    assert code(client.post("/v1/assurance/runs", json=run_body | {"project_id": other_project},
                            headers=key)) == (404, "plans.not_found")  # fmt: skip
    assert code(client.get("/v1/members", headers=key)) == (403, "auth.key_scope")
    assert code(client.get(f"/v1/projects/{lab.project}", headers=key)) == (403, "auth.key_scope")
    assert code(client.get("/v1/api-keys", headers=key)) == (403, "auth.key_scope")
    assert code(client.get("/v1/me", headers=key)) == (403, "auth.key_not_allowed")
    assert code(client.post("/v1/assurance/runs", json={}, headers=key))[0] == 422
    wrong_tenant = key | {"X-RAI-Tenant": str(stranger_tenant.tenant_id)}
    assert code(client.get("/v1/assurance/runs", headers=wrong_tenant)) == (404, "tenant.not_found")
    bad = apikeys.generate().token
    assert code(client.get("/v1/assurance/runs", headers=bearer(bad))) == (401, "auth.invalid_key")
    assert code(client.get("/v1/assurance/runs", headers=bearer("rai_short"))) == (
        401, "auth.invalid_key",
    )  # fmt: skip

    # Another project's key sees none of this project's runs.
    other_key = bearer(new_key(lab, project=other_project)["key"])
    run_id = started.json()["run_id"]
    assert client.get(f"/v1/assurance/runs/{run_id}", headers=other_key).status_code == 404
    for path in ("observations", "lineage"):
        url = f"/v1/assurance/runs/{run_id}/{path}"
        assert client.get(url, headers=other_key).status_code == 404
    assert client.get("/v1/assurance/runs", headers=other_key).json()["items"] == []
    mine = client.get("/v1/assurance/runs", params={"project_id": other_project}, headers=key)
    assert mine.json()["items"] == []
    # Replaying another project's Idempotency-Key never returns that project's run.
    same = {"plan_id": lab.plan(lab.tests["batch_0"])}
    once = {"Idempotency-Key": "ci-1"}
    assert client.post("/v1/assurance/runs", json=same, headers=key | once).status_code == 202
    sneaky = client.post("/v1/assurance/runs", json=same, headers=other_key | once)
    assert code(sneaky) == (422, "idempotency.key_reused")


def test_a_key_stops_working_when_its_creator_loses_access(
    lab: Lab, client: TestClient, owner: Engine
) -> None:
    key = bearer(new_key(lab)["key"])
    assert client.get("/v1/assurance/runs", headers=key).status_code == 200
    creator = client.get("/v1/me", headers=lab.engineer).json()["user_id"]
    admin = auth(login(client, lab.tenant.admin_email), lab.tenant.tenant_id)
    assert client.delete(f"/v1/members/{creator}", headers=admin).status_code == 204
    assert client.get("/v1/assurance/runs", headers=key).status_code == 401


NOW = datetime.now(UTC)


def test_an_expired_key_is_refused(lab: Lab, client: TestClient, owner: Engine) -> None:
    made = apikeys.generate()
    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rai.api_keys (tenant_id, project_id, name, key_prefix, key_hash, "
                "scopes, created_by, created_at, expires_at) SELECT tenant_id, project_id, 'old', "
                ":prefix, :h, ARRAY['runs:read']::rai.api_key_scope[], owner_id, :c, :e "
                "FROM rai.assurance_projects WHERE project_id = :p"
            ),
            {"prefix": made.prefix, "h": made.secret_hash, "p": lab.project,
             "c": NOW - timedelta(days=2), "e": NOW - timedelta(days=1)},
        )  # fmt: skip
    assert client.get("/v1/assurance/runs", headers=bearer(made.token)).status_code == 401


def test_the_database_keeps_keys_honest(lab: Lab, owner: Engine) -> None:
    key_id = new_key(lab)["api_key_id"]
    with owner.begin() as conn, pytest.raises(DBAPIError, match="can only be revoked"):
        conn.execute(
            text("UPDATE rai.api_keys SET expires_at = expires_at + interval '1 day' "
                 "WHERE api_key_id = :k"), {"k": key_id},
        )  # fmt: skip
    with owner.begin() as conn, pytest.raises(DBAPIError, match="insert-only"):
        conn.execute(text("DELETE FROM rai.api_keys WHERE api_key_id = :k"), {"k": key_id})
    made = apikeys.generate()
    with owner.begin() as conn, pytest.raises(DBAPIError, match="api_keys_check"):
        conn.execute(
            text(
                "INSERT INTO rai.api_keys (tenant_id, project_id, name, key_prefix, key_hash, "
                "scopes, created_by, expires_at) SELECT tenant_id, project_id, 'long', :prefix, "
                ":h, ARRAY['runs:read']::rai.api_key_scope[], owner_id, "
                "clock_timestamp() + interval '91 days' "
                "FROM rai.assurance_projects WHERE project_id = :p"
            ),
            {"prefix": made.prefix, "h": made.secret_hash, "p": lab.project},
        )


# ---- rate limit ------------------------------------------------------------------------------


@pytest.fixture
def strict_client(api_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=api_url, jwt_secret=SECRET, key_requests_per_minute=2)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_each_key_has_a_request_limit(lab: Lab, strict_client: TestClient) -> None:
    key, other = bearer(new_key(lab)["key"]), bearer(new_key(lab)["key"])
    for _ in range(2):
        assert strict_client.get("/v1/assurance/runs", headers=key).status_code == 200
    limited = strict_client.get("/v1/assurance/runs", headers=key)
    assert (limited.status_code, limited.json()["code"]) == (429, "auth.key_rate_limited")
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    # The limit is per key, not per project or tenant.
    assert strict_client.get("/v1/assurance/runs", headers=other).status_code == 200
