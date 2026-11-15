"""Declared inputs and run lineage with content digests (B-19: FR-04.1).

Acceptance (spec FR-04.1): the lineage graph for a run shows all hops and the
SHA-256 of its inputs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from rai.lineage.digest import frame_digest
from rai.worker.service import Worker
from tests.integration.apikit import needs_db, new_tenant, signed_in_member
from tests.integration.runkit import TXN_YAML, Lab, config, drain

pytestmark = needs_db

ETL = "etl-job@4f2a9c1"


def digest(path: Path) -> str:
    """Computed here independently of the worker: the CSV as the connector reads it."""
    return frame_digest(pl.read_csv(path, infer_schema=False))


def rows(path: Path) -> int:
    return pl.read_csv(path, infer_schema=False).height  # B000001 also has re-sent rows


def asset(lab: Lab, kind: str, name: str, locator: str | None = None) -> str:
    body = {"kind": kind, "name": name} | ({"locator": locator} if locator else {})
    asset_id: str = lab.post(f"/v1/projects/{lab.project}/assets", body)["asset_id"]
    return asset_id


def declare(lab: Lab, asset_id: str, input_asset_id: str, transform_ref: str | None = None) -> Any:
    body = {"input_asset_id": input_asset_id} | (
        {"transform_ref": transform_ref} if transform_ref else {}
    )
    return lab.post(f"/v1/assets/{asset_id}/inputs", body)


def lineage(lab: Lab, run_id: str) -> dict[str, Any]:
    response = lab.client.get(f"/v1/assurance/runs/{run_id}/lineage", headers=lab.viewer)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    """A second source the operator configures on the worker: ledger postings and a
    feature extract, synthetic."""
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "postings.csv").write_text(
        "posting_id,account_id,amount\nP1,A1,12.50\nP2,A2,-3.00\nP3,A1,\n", encoding="utf-8"
    )
    return root


def ledger_connector(root: Path) -> dict[str, Any]:
    return {"name": "ledger", "environment": "SANDBOX-01", "type": "csv", "root": root.as_posix()}


# ---- acceptance ------------------------------------------------------------------------------


def test_a_runs_lineage_shows_every_hop_and_the_sha256_of_its_inputs(
    lab: Lab, worker_engine: Engine, feed: Path, ledger: Path, tmp_path: Path
) -> None:
    """FR-04.1 acceptance: source -> transform -> table -> feature set -> run."""
    postings = asset(lab, "source", "ledger_postings", "postings.csv")
    etl = asset(lab, "transform", "etl_job")
    table = asset(lab, "table", "txn_table", "transactions_raw/B000001.csv")
    features = asset(lab, "feature_set", "txn_features", "transactions_raw/B000000.csv")
    model = asset(lab, "model", "fraud_model")
    lab.post(f"/v1/assets/{features}/contracts", {"source": TXN_YAML})
    declare(lab, etl, postings)
    declare(lab, table, etl, ETL)
    declare(lab, features, table, "features@9b3e")
    declare(lab, model, features)  # downstream of the target: not this run's lineage
    test = lab.test_case(
        "features", features, upstream={"ledger_postings": "ledger", "txn_table": "feed"}
    )
    run = lab.start(lab.plan(test))

    pinned = lab.run(run["run_id"])["pins"]["items"][0]
    assert {(hop["source"]["name"], hop["transform_ref"]) for hop in pinned["upstream"]} == {
        ("ledger_postings", None), ("etl_job", ETL), ("txn_table", "features@9b3e"),
    }  # fmt: skip
    assert pinned["asset"]["kind"] == "feature_set"

    drain(Worker(worker_engine, config(feed, tmp_path / "state", ledger=ledger_connector(ledger))))
    assert lab.run(run["run_id"])["state"] == "passed"

    graph = lineage(lab, run["run_id"])
    assert graph["digest_format"] == "rai-frame-digest/1"
    names = {a["asset_id"]: a["name"] for a in graph["assets"]}
    assert sorted(names.values()) == ["etl_job", "ledger_postings", "txn_features", "txn_table"]
    batches = feed / "feed" / "transactions_raw"
    hops = {
        (names.get(e["from_asset_id"], e["from_name"]), names.get(e["to_asset_id"], "run")):
            (e["from_digest"], e["from_rows"], e["to_digest"], e["transform_ref"])
        for e in graph["edges"]
    }  # fmt: skip
    assert hops == {
        ("ledger_postings", "etl_job"): (digest(ledger / "postings.csv"), 3, None, None),
        ("etl_job", "txn_table"): (None, None, digest(batches / "B000001.csv"), ETL),
        ("txn_table", "txn_features"):
            (digest(batches / "B000001.csv"), rows(batches / "B000001.csv"),
             digest(batches / "B000000.csv"),
             "features@9b3e"),
        ("txn_features", "run"): (digest(batches / "B000000.csv"), 2_000, None, None),
        ("reference:accounts", "run"): (digest(feed / "feed" / "accounts.csv"), 200, None, None),
        ("reference:merchants", "run"): (digest(feed / "feed" / "merchants.csv"), 40, None, None),
    }  # fmt: skip
    assert {e["step_key"] for e in graph["edges"]} == {"item-1"}


# ---- runs ------------------------------------------------------------------------------------


def test_unmapped_upstream_assets_are_hops_without_a_digest(
    lab: Lab, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    source = asset(lab, "source", "ledger_postings", "postings.csv")
    batch_0 = lab.assets["batch_0"]
    declare(lab, batch_0, source, ETL)
    run = lab.start(lab.plan(lab.test_case("batch_0", batch_0)))
    drain(Worker(worker_engine, config(feed, tmp_path)))
    edges = {e["from_name"]: e for e in lineage(lab, run["run_id"])["edges"]}
    assert (edges["ledger_postings"]["from_digest"], edges["ledger_postings"]["to_digest"]) == (
        None, digest(feed / "feed" / "transactions_raw" / "B000000.csv"),
    )  # fmt: skip
    assert edges["batch_0"]["from_rows"] == 2_000


def test_a_run_keeps_the_graph_it_was_queued_with(
    lab: Lab, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    source = asset(lab, "source", "ledger_postings")
    batch_0 = lab.assets["batch_0"]
    declared = declare(lab, batch_0, source)
    run = lab.start(lab.plan(lab.tests["batch_0"]))
    removed = lab.client.delete(
        f"/v1/assets/{batch_0}/inputs/{declared['input_id']}", headers=lab.engineer
    )
    assert removed.status_code == 204
    drain(Worker(worker_engine, config(feed, tmp_path)))
    assert "ledger_postings" in {e["from_name"] for e in lineage(lab, run["run_id"])["edges"]}
    later = lab.start(lab.plan(lab.tests["batch_0"]))
    drain(Worker(worker_engine, config(feed, tmp_path)))
    assert "ledger_postings" not in {e["from_name"] for e in lineage(lab, later["run_id"])["edges"]}


def test_preflight_refuses_upstream_it_cannot_read(
    lab: Lab, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    source = asset(lab, "source", "ledger_postings", "postings.csv")
    no_locator = asset(lab, "transform", "etl_job")
    batch_0 = lab.assets["batch_0"]
    declare(lab, batch_0, source)
    declare(lab, batch_0, no_locator)
    test = lab.test_case(
        "batch_0", batch_0,
        upstream={"ledger_postings": "ledger", "etl_job": "feed", "unrelated": "feed"},
    )  # fmt: skip
    run = lab.start(lab.plan(test))
    drain(Worker(worker_engine, config(feed, tmp_path)))
    refused = lab.run(run["run_id"])
    assert refused["state"] == "refused"
    assert "upstream asset 'etl_job' has no locator" in refused["reason"]
    assert (
        "upstream 'ledger_postings': connector 'ledger' is not on this worker" in refused["reason"]
    )
    assert "'unrelated' is not upstream of 'batch_0'" in refused["reason"]
    assert lineage(lab, run["run_id"])["edges"] == []


def test_a_step_that_could_not_read_records_no_lineage(
    lab: Lab, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    run = lab.start(lab.plan(lab.tests["missing"]))
    drain(Worker(worker_engine, config(feed, tmp_path)))
    assert lab.run(run["run_id"])["steps"][0]["state"] == "error"
    assert lineage(lab, run["run_id"]) | {"run_id": None} == {
        "run_id": None, "digest_format": "rai-frame-digest/1", "assets": [], "edges": [],
    }  # fmt: skip


def test_lineage_edges_are_insert_only_and_tenant_scoped(
    lab: Lab, client: TestClient, owner: Engine, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    run = lab.start(lab.plan(lab.tests["batch_0"]))
    drain(Worker(worker_engine, config(feed, tmp_path)))
    assert len(lineage(lab, run["run_id"])["edges"]) == 3  # the batch and two references
    with owner.begin() as conn, pytest.raises(DBAPIError, match="insert-only"):
        conn.execute(
            text("UPDATE rai.lineage_edges SET from_digest = repeat('0', 64) WHERE run_id = :r"),
            {"r": run["run_id"]},
        )
    other = new_tenant(owner)
    stranger = signed_in_member(client, owner, other, "admin")
    response = client.get(f"/v1/assurance/runs/{run['run_id']}/lineage", headers=stranger)
    assert response.status_code == 404


# ---- declaring inputs ------------------------------------------------------------------------


def test_declaring_inputs(lab: Lab, client: TestClient, owner: Engine) -> None:
    source = asset(lab, "source", "ledger_postings")
    etl = asset(lab, "transform", "etl_job")
    table = asset(lab, "table", "txn_table")
    first = declare(lab, etl, source)
    second = declare(lab, table, etl, ETL)
    assert (second["input_name"], second["input_kind"], second["transform_ref"]) == (
        "etl_job", "transform", ETL,
    )  # fmt: skip
    listed = client.get(f"/v1/assets/{table}/inputs", headers=lab.viewer).json()
    assert [i["input_asset_id"] for i in listed["items"]] == [etl]

    def refused(asset_id: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        return client.post(
            f"/v1/assets/{asset_id}/inputs", json=body, headers=headers or lab.engineer
        )

    duplicate = refused(table, {"input_asset_id": etl})
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "lineage.duplicate_input")
    cycle = refused(source, {"input_asset_id": table})
    assert (cycle.status_code, cycle.json()["code"]) == (422, "lineage.cycle")
    assert (
        cycle.json()["detail"] == "ledger_postings already feeds txn_table, directly or indirectly"
    )
    itself = refused(table, {"input_asset_id": table})
    assert (itself.status_code, itself.json()["code"]) == (422, "lineage.cycle")
    elsewhere = lab.post("/v1/projects", {"name": f"q-{uuid4().hex[:6]}"})["project_id"]
    foreign = lab.post(f"/v1/projects/{elsewhere}/assets", {"kind": "source", "name": "s"})
    across = refused(table, {"input_asset_id": foreign["asset_id"]})
    assert (across.status_code, across.json()["code"]) == (422, "lineage.other_project")
    assert refused(table, {"input_asset_id": source}, lab.viewer).status_code == 403

    other = new_tenant(owner)
    stranger = signed_in_member(client, owner, other, "engineer")
    assert client.get(f"/v1/assets/{table}/inputs", headers=stranger).status_code == 404
    assert refused(table, {"input_asset_id": source}, stranger).status_code == 404

    gone = client.delete(f"/v1/assets/{etl}/inputs/{first['input_id']}", headers=lab.engineer)
    assert gone.status_code == 204
    again = client.delete(f"/v1/assets/{etl}/inputs/{first['input_id']}", headers=lab.engineer)
    assert again.status_code == 404
    with owner.connect() as conn:
        actions = conn.execute(
            text(
                "SELECT action FROM rai.audit_chain WHERE subject_type = 'asset_input' "
                "AND subject_id = :i ORDER BY seq"
            ),
            {"i": first["input_id"]},
        ).scalars().all()  # fmt: skip
    assert actions == ["asset_input.insert", "asset_input.delete"]


def test_the_database_refuses_cycles_and_cross_tenant_inputs(lab: Lab, owner: Engine) -> None:
    a = asset(lab, "table", "a")
    b = asset(lab, "table", "b")
    c = asset(lab, "table", "c")
    declare(lab, b, a)
    declare(lab, c, b)
    with owner.begin() as conn, pytest.raises(DBAPIError, match="would close a cycle"):
        conn.execute(
            text(
                "INSERT INTO rai.asset_inputs (tenant_id, project_id, asset_id, input_asset_id, "
                "created_by) SELECT tenant_id, project_id, :a, :c, created_by "
                "FROM rai.pipeline_assets WHERE asset_id = :a"
            ),
            {"a": a, "c": c},
        )


def test_lineage_needs_a_signed_in_member(lab: Lab, client: TestClient) -> None:
    run = lab.start(lab.plan(lab.tests["batch_0"]))
    assert client.get(f"/v1/assurance/runs/{run['run_id']}/lineage").status_code == 401


def test_an_oversized_upstream_graph_is_refused_when_queued(lab: Lab, owner: Engine) -> None:
    with owner.begin() as conn:  # 101 sources feeding one batch, faster than 202 API calls
        conn.execute(
            text(
                "WITH target AS (SELECT tenant_id, project_id, created_by "
                "FROM rai.pipeline_assets WHERE asset_id = :a), "
                "sources AS (INSERT INTO rai.pipeline_assets "
                "(tenant_id, project_id, kind, name, created_by) "
                "SELECT tenant_id, project_id, 'source', 'src_' || n, created_by "
                "FROM target, generate_series(1, 101) AS n "
                "RETURNING tenant_id, project_id, asset_id, created_by) "
                "INSERT INTO rai.asset_inputs (tenant_id, project_id, asset_id, input_asset_id, "
                "created_by) SELECT tenant_id, project_id, :a, asset_id, created_by FROM sources"
            ),
            {"a": lab.assets["batch_0"]},
        )
    response = lab.client.post(
        "/v1/assurance/runs", json={"plan_id": lab.plan(lab.tests["batch_0"])},
        headers=lab.engineer,
    )  # fmt: skip
    assert (response.status_code, response.json()["code"]) == (422, "runs.lineage_too_large")
