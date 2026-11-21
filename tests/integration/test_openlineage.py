"""OpenLineage ingest (FR-04.2): an external orchestrator's job appears in the lineage
graph. Events come with a project API key scoped ``lineage:write``; RAI keeps their core
(never the facets), stores a retried event once, and links datasets to mapped assets."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab
from tests.integration.test_api_keys import bearer, new_key

pytestmark = needs_db

SQL = "SELECT * FROM ledger.transactions WHERE password = 'hunter2'"  # a facet RAI drops


def event(run_id: str, kind: str = "COMPLETE", **extra: Any) -> dict[str, Any]:
    """A RunEvent as Airflow's OpenLineage provider sends it."""
    return {
        "eventType": kind, "eventTime": "2026-11-21T03:00:00.000Z",
        "producer": "https://github.com/apache/airflow/tree/providers-openlineage/2.3.0",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
        "run": {"runId": run_id, "facets": {"nominalTime": {"nominalStartTime": "2026-11-21"}}},
        "job": {"namespace": "airflow-prod", "name": "settlement.load_warehouse",
                "facets": {"sql": {"query": SQL}}},
        "inputs": [{"namespace": "postgres://ledger-db:5432", "name": "ledger.public.transactions",
                    "facets": {"schema": {"fields": [{"name": "txn_id", "type": "uuid"}]}}}],
        "outputs": [{"namespace": "postgres://analytics-db:5432",
                     "name": "analytics.warehouse.fact_transaction"}],
    } | extra  # fmt: skip


def graph(lab: Lab, client: TestClient) -> dict[str, Any]:
    response = client.get(f"/v1/projects/{lab.project}/lineage", headers=lab.viewer)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def test_an_orchestrators_job_appears_in_the_lineage_graph(
    lab: Lab, client: TestClient, owner: Engine
) -> None:
    key = new_key(lab, scopes=["lineage:write"])
    assert key["scopes"] == ["lineage:write"]
    url = f"/v1/projects/{lab.project}/openlineage"
    run_id = str(uuid4())
    for kind in ("START", "COMPLETE"):
        stored = client.post(url, json=event(run_id, kind), headers=bearer(key["key"]))
        assert stored.status_code == 201, stored.text
        assert stored.json()["stored"] is True

    # Map the job's output to the warehouse table RAI tests, and its input to the ledger.
    table = lab.post(f"/v1/projects/{lab.project}/assets",
                     {"kind": "table", "name": "fact_transaction"})["asset_id"]  # fmt: skip
    mapped = client.post(f"/v1/assets/{table}/openlineage",
                         json={"namespace": "postgres://analytics-db:5432",
                               "name": "analytics.warehouse.fact_transaction"},
                         headers=lab.engineer)  # fmt: skip
    assert mapped.status_code == 201, mapped.text

    seen = graph(lab, client)
    (job,) = seen["jobs"]
    assert (job["namespace"], job["name"], job["runs"]) == (
        "airflow-prod", "settlement.load_warehouse", 1)  # fmt: skip
    assert job["last_event"]["event_type"] == "COMPLETE"
    assert job["last_event"]["run_id"] == run_id
    assert job["inputs"] == [{"namespace": "postgres://ledger-db:5432",
                              "name": "ledger.public.transactions", "asset_id": None}]  # fmt: skip
    assert job["outputs"] == [{"namespace": "postgres://analytics-db:5432",
                               "name": "analytics.warehouse.fact_transaction",
                               "asset_id": table}]  # fmt: skip
    asset = next(a for a in seen["assets"] if a["asset_id"] == table)
    assert asset["openlineage"]["name"] == "analytics.warehouse.fact_transaction"

    # Facets are never stored: only their names. The SQL with its password is not kept.
    with owner.connect() as conn:
        rows = conn.execute(
            text("SELECT e.facet_names, to_jsonb(e)::text AS everything "
                 "FROM rai.openlineage_events e WHERE e.project_id = :p"), {"p": lab.project},
        ).all()  # fmt: skip
        audited = conn.execute(
            text("SELECT count(*) FROM rai.audit_chain WHERE action = 'openlineage_event.insert' "
                 "AND tenant_id = :t AND actor_type = 'api_key'"),
            {"t": lab.tenant.tenant_id},
        ).scalar_one()  # fmt: skip
    assert len(rows) == 2
    assert set(rows[0].facet_names) == {"dataset.schema", "job.sql", "run.nominalTime"}
    assert all("hunter2" not in r.everything for r in rows)
    assert audited == 2


def test_a_retried_event_is_stored_once(lab: Lab, client: TestClient) -> None:
    key = bearer(new_key(lab, scopes=["lineage:write"])["key"])
    url = f"/v1/projects/{lab.project}/openlineage"
    body = event(str(uuid4()))
    first = client.post(url, json=body, headers=key)
    again = client.post(url, json=body, headers=key)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == {"event_id": first.json()["event_id"], "stored": False}
    events = client.get(f"{url}/events", headers=lab.viewer).json()["items"]
    assert len(events) == 1
    assert events[0]["outputs"][0]["asset_id"] is None


def test_only_a_lineage_key_of_this_project_or_an_engineer_may_post(
    lab: Lab, client: TestClient, owner: Engine
) -> None:
    url = f"/v1/projects/{lab.project}/openlineage"
    body = event(str(uuid4()))
    runs_key = bearer(new_key(lab)["key"])  # the default scopes are CI's, not lineage
    refused = client.post(url, json=body, headers=runs_key)
    assert (refused.status_code, refused.json()["code"]) == (403, "auth.key_scope")
    assert client.post(url, json=body, headers=lab.viewer).status_code == 403
    assert client.post(url, json=body, headers=lab.engineer).status_code == 201

    other = lab.post("/v1/projects", {"name": f"p-{uuid4().hex[:6]}"})["project_id"]
    foreign = bearer(new_key(lab, project=other, scopes=["lineage:write"])["key"])
    assert client.post(url, json=event(str(uuid4())), headers=foreign).status_code == 404


def test_an_event_that_is_not_a_run_event_is_refused(lab: Lab, client: TestClient) -> None:
    url = f"/v1/projects/{lab.project}/openlineage"
    good = event(str(uuid4()))
    too_many = [{"namespace": "x", "name": f"t{i}"} for i in range(201)]
    for bad in ({**good, "run": {"runId": "not-a-uuid"}},
                {**good, "eventType": "FINISHED"},
                {k: v for k, v in good.items() if k != "job"},
                {**good, "inputs": [{"namespace": "x", "name": ""}]},
                {**good, "outputs": too_many}):  # fmt: skip
        assert client.post(url, json=bad, headers=lab.engineer).status_code == 422


def test_a_dataset_maps_to_one_asset_and_back(lab: Lab, client: TestClient) -> None:
    ns = {"namespace": "s3://bucket", "name": "exports/daily"}
    one = lab.post(f"/v1/projects/{lab.project}/assets", {"kind": "table", "name": "one"})
    two = lab.post(f"/v1/projects/{lab.project}/assets", {"kind": "table", "name": "two"})
    path = f"/v1/assets/{one['asset_id']}/openlineage"
    assert client.post(path, json=ns, headers=lab.engineer).status_code == 201
    assert client.get(path, headers=lab.viewer).json()["name"] == "exports/daily"
    taken = client.post(f"/v1/assets/{two['asset_id']}/openlineage", json=ns, headers=lab.engineer)
    assert (taken.status_code, taken.json()["code"]) == (409, "openlineage.already_mapped")
    assert client.post(path, json=ns | {"name": "x"}, headers=lab.engineer).status_code == 409
    assert client.post(path, json=ns, headers=lab.viewer).status_code == 403
    assert client.delete(path, headers=lab.engineer).status_code == 204
    assert client.get(path, headers=lab.viewer).status_code == 404
    moved = client.post(f"/v1/assets/{two['asset_id']}/openlineage", json=ns, headers=lab.engineer)
    assert moved.status_code == 201


def test_another_tenant_sees_nothing(lab: Lab, client: TestClient, owner: Engine) -> None:
    lab_events = f"/v1/projects/{lab.project}/openlineage"
    assert (
        client.post(lab_events, json=event(str(uuid4())), headers=lab.engineer).status_code == 201
    )
    stranger = Lab(client, owner)
    assert client.get(f"/v1/projects/{lab.project}/lineage",
                      headers=stranger.viewer).status_code == 404  # fmt: skip
    assert graph(stranger, client)["jobs"] == []
