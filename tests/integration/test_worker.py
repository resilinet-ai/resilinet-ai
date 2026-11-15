"""Runs, the job queue and the worker against PostgreSQL 16 (B-18: FR-06.1, FR-06.2).

Acceptance (spec FR-06.2): killing the worker mid-run does not duplicate
observations; the resumed run reaches the same verdict as an uninterrupted one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from rai.cli.main import app as cli
from rai.worker import queue
from rai.worker.executor import Executor, WorkerConfig
from rai.worker.service import Worker, WorkerSetupError
from sandboxbank.fixtures import build_slice
from tests.integration.apikit import (
    DSN,
    WORKER_PASSWORD,
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
RULES = 36  # rules in transactions_raw@1


@pytest.fixture(scope="module")
def feed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("slice")
    build_slice(
        out, data_seed=27, injector_seed=28, accounts=200, merchants=40, rows_per_batch=2_000
    )
    return out


@pytest.fixture(scope="module")
def seeded(feed: Path) -> int:
    truth = (feed / "answer_key" / "ground_truth.jsonl").read_text("utf-8").splitlines()
    return len(json.loads(truth[1])["rows"])


@pytest.fixture(scope="module")
def worker_engine(worker_url: str) -> Iterator[Engine]:
    engine = create_engine(worker_url)
    yield engine
    engine.dispose()


def config(feed: Path, state: Path, name: str | None = None) -> WorkerConfig:
    connector = {"name": "feed", "environment": "SANDBOX-01", "type": "csv",
                 "root": (feed / "feed").as_posix()}  # fmt: skip
    return WorkerConfig(name=name or f"w-{uuid4().hex[:6]}", state_dir=state,
                        connectors={"feed": connector})  # fmt: skip


class Lab:
    """A tenant with a project, two batch assets with contracts, and test cases."""

    def __init__(self, client: TestClient, owner: Engine) -> None:
        self.client = client
        self.tenant: Tenant = new_tenant(owner)
        self.engineer = signed_in_member(client, owner, self.tenant, "engineer")
        self.viewer = signed_in_member(client, owner, self.tenant, "viewer")
        self.project = self.post("/v1/projects", {"name": f"p-{uuid4().hex[:6]}"})["project_id"]
        self.tests: dict[str, str] = {}
        for name, locator in (("batch_0", "transactions_raw/B000000.csv"),
                              ("batch_1", "transactions_raw/B000001.csv"),
                              ("missing", "transactions_raw/B999999.csv")):  # fmt: skip
            asset = self.post(f"/v1/projects/{self.project}/assets",
                              {"kind": "table", "name": name, "locator": locator})  # fmt: skip
            self.post(f"/v1/assets/{asset['asset_id']}/contracts", {"source": TXN_YAML})
            self.tests[name] = self.test_case(name, asset["asset_id"])

    def post(self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        response = self.client.post(url, json=body, headers=headers or self.engineer)
        assert response.status_code in (201, 202), response.text
        return response.json()

    def test_case(self, name: str, asset_id: str, **definition: Any) -> str:
        body = {
            "name": f"contract.{name}", "type": "contract_rule", "target_asset_id": asset_id,
            "definition": {"connector": "feed", "environment": "SANDBOX-01",
                           "references": {"accounts": "accounts.csv", "merchants": "merchants.csv"}}
            | definition,
        }  # fmt: skip
        test_id: str = self.post(f"/v1/projects/{self.project}/test-cases", body)["test_id"]
        return test_id

    def plan(self, *test_ids: str) -> str:
        body = {"name": f"plan-{uuid4().hex[:6]}", "items": [{"test_id": t} for t in test_ids]}
        plan_id: str = self.post(f"/v1/projects/{self.project}/plans", body)["plan_id"]
        return plan_id

    def start(self, plan_id: str, **headers: str) -> dict[str, Any]:
        result: dict[str, Any] = self.post(
            "/v1/assurance/runs", {"plan_id": plan_id}, self.engineer | headers
        )
        return result

    def run(self, run_id: str) -> dict[str, Any]:
        response = self.client.get(f"/v1/assurance/runs/{run_id}", headers=self.viewer)
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result


@pytest.fixture
def lab(client: TestClient, owner: Engine) -> Lab:
    return Lab(client, owner)


def drain(worker: Worker) -> None:
    while worker.run_once():
        pass


def rule_counts(owner: Engine, run_id: str) -> dict[str, int]:
    with owner.connect() as conn:
        rows = conn.execute(
            text("SELECT step_key, count(*) FROM rai.observations WHERE run_id = :r GROUP BY 1"),
            {"r": run_id},
        ).all()
    return {step: int(n) for step, n in rows}


# ---- the run lifecycle -----------------------------------------------------------------------


def test_a_run_validates_its_batches_and_catches_the_seeded_duplicates(
    lab: Lab, worker_engine: Engine, feed: Path, tmp_path: Path, seeded: int
) -> None:
    clean = lab.start(lab.plan(lab.tests["batch_0"]))
    assert (clean["state"], clean["verdict"]) == ("queued", None)
    both = lab.start(lab.plan(lab.tests["batch_0"], lab.tests["batch_1"]))
    drain(Worker(worker_engine, config(feed, tmp_path)))

    passed = lab.run(clean["run_id"])
    assert (passed["state"], passed["verdict"]) == ("passed", "pass")
    assert passed["observations"] == {"pass": RULES}
    assert passed["started_at"]
    assert passed["finished_at"]
    failed = lab.run(both["run_id"])
    assert (failed["state"], failed["verdict"]) == ("failed", "fail")
    assert [(s["step_key"], s["verdict"]) for s in failed["steps"]] == [
        ("item-1", "pass"), ("item-2", "fail"),
    ]  # fmt: skip
    pins = failed["pins"]
    assert pins["items"][1]["contract"]["ref"] == "transactions_raw@1"
    bad = lab.client.get(
        f"/v1/assurance/runs/{both['run_id']}/observations", params={"outcome": "fail"},
        headers=lab.viewer,
    ).json()["items"]  # fmt: skip
    assert [(o["rule"], o["failing_count"]) for o in bad] == [
        ("unique_across_batches:txn_id", seeded)
    ]
    assert bad[0]["expected"]["contract"] == "transactions_raw@1"


def test_killing_the_worker_mid_run_does_not_duplicate_observations(
    lab: Lab, worker_engine: Engine, owner: Engine, feed: Path, tmp_path: Path
) -> None:
    """FR-06.2 acceptance."""
    drain(Worker(worker_engine, config(feed, tmp_path / "leftovers")))  # only our run is queued
    run = lab.start(lab.plan(lab.tests["batch_0"], lab.tests["batch_1"]))

    def die_in_step_two(step: str) -> None:
        if step == "item-2":
            raise SystemExit("worker killed")  # not an Exception: nothing handles it

    first_config = config(feed, tmp_path)
    first = Worker(worker_engine, first_config)
    first.executor = Executor(worker_engine, first_config, renew=first.renew,
                              before_commit=die_in_step_two)  # fmt: skip
    with pytest.raises(SystemExit):
        first.run_once()
    assert rule_counts(owner, run["run_id"]) == {"item-1": RULES}  # step 2 rolled back
    assert lab.run(run["run_id"])["state"] == "running"

    with owner.begin() as conn:  # the dead worker's lease runs out
        conn.execute(
            text("UPDATE rai.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' "
                 "WHERE run_id = :r"),
            {"r": run["run_id"]},
        )  # fmt: skip
    second = Worker(worker_engine, config(feed, tmp_path))
    assert second.run_once()
    done = lab.run(run["run_id"])
    assert (done["state"], done["verdict"]) == ("failed", "fail")
    assert rule_counts(owner, run["run_id"]) == {"item-1": RULES, "item-2": RULES}
    assert [(s["step_key"], s["attempt"]) for s in done["steps"]] == [("item-1", 1), ("item-2", 2)]

    stale = queue.Job(uuid4(), lab.tenant.tenant_id, uuid4(), 1, 3)
    assert not queue.renew(worker_engine, stale, first_config.name, first_config.lease)

    uninterrupted = lab.start(lab.plan(lab.tests["batch_0"], lab.tests["batch_1"]))
    drain(Worker(worker_engine, config(feed, tmp_path / "fresh")))
    assert lab.run(uninterrupted["run_id"])["verdict"] == done["verdict"]


def test_a_worker_that_lost_its_lease_stops_without_touching_the_run(
    lab: Lab, worker_engine: Engine, owner: Engine, feed: Path, tmp_path: Path
) -> None:
    drain(Worker(worker_engine, config(feed, tmp_path / "leftovers")))
    run = lab.start(lab.plan(lab.tests["batch_0"]))
    cfg = config(feed, tmp_path)
    worker = Worker(worker_engine, cfg)
    worker.executor = Executor(worker_engine, cfg, renew=lambda _: False)  # taken over
    assert worker.run_once()
    assert lab.run(run["run_id"])["state"] == "running"  # left for the new owner
    assert rule_counts(owner, run["run_id"]) == {}
    with owner.connect() as conn:
        job = conn.execute(text("SELECT finished_at, locked_by FROM rai.jobs WHERE run_id = :r"),
                           {"r": run["run_id"]}).one()  # fmt: skip
    assert (job.finished_at, job.locked_by) == (None, cfg.name)


def test_a_failing_platform_is_retried_then_the_run_errors(
    lab: Lab, worker_engine: Engine, owner: Engine, feed: Path, tmp_path: Path
) -> None:
    drain(Worker(worker_engine, config(feed, tmp_path / "leftovers")))  # only our run is queued
    run = lab.start(lab.plan(lab.tests["batch_0"]))
    cfg = config(feed, tmp_path)
    worker = Worker(worker_engine, cfg)

    def broken(_: str) -> None:
        raise RuntimeError("disk full")

    worker.executor = Executor(worker_engine, cfg, renew=worker.renew, before_commit=broken)
    for _ in range(3):
        assert worker.run_once()
        with owner.begin() as conn:  # skip the back-off
            conn.execute(text("UPDATE rai.jobs SET available_at = clock_timestamp() "
                              "WHERE run_id = :r"), {"r": run["run_id"]})  # fmt: skip
    assert not worker.run_once()  # the sweep closes it; nothing left to claim
    ended = lab.run(run["run_id"])
    assert ended["state"] == "error"
    assert "failed 3 times" in ended["reason"]
    assert "RuntimeError: disk full" in ended["reason"]
    assert ended["observations"] == {}


def test_preflight_refuses_what_this_worker_cannot_run(
    lab: Lab, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    asset_id = lab.client.get(f"/v1/projects/{lab.project}/assets", headers=lab.viewer).json()[
        "items"
    ][0]["asset_id"]
    unknown = lab.test_case("elsewhere", asset_id, connector="warehouse")
    other_env = lab.test_case("staging", asset_id, environment="STAGING-01")
    no_definition = lab.post(f"/v1/projects/{lab.project}/test-cases",
                             {"name": "contract.bare", "type": "contract_rule",
                              "target_asset_id": asset_id})["test_id"]  # fmt: skip
    drift = lab.post(f"/v1/projects/{lab.project}/test-cases",
                     {"name": "drift.amount", "type": "drift_check"})["test_id"]  # fmt: skip
    run = lab.start(lab.plan(lab.tests["batch_0"], unknown, other_env, no_definition, drift))
    drain(Worker(worker_engine, config(feed, tmp_path)))
    refused = lab.run(run["run_id"])
    assert (refused["state"], refused["verdict"], refused["steps"]) == ("refused", None, [])
    reason = refused["reason"]
    assert "connector 'warehouse' is not on this worker" in reason
    assert "is not in environment 'STAGING-01'" in reason
    assert "the definition needs connector and environment" in reason
    assert "test type drift_check is not supported yet" in reason


def test_unreadable_data_fails_the_step_not_the_platform(
    lab: Lab, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    run = lab.start(lab.plan(lab.tests["missing"]))
    drain(Worker(worker_engine, config(feed, tmp_path)))
    ended = lab.run(run["run_id"])
    assert (ended["state"], ended["verdict"]) == ("failed", "fail")
    assert ended["steps"][0]["state"] == "error"
    assert "could not read the data" in ended["steps"][0]["error"]
    assert ended["observations"] == {"error": 1}


def test_freshness_can_be_measured_against_the_run_clock(
    lab: Lab, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    asset_id = lab.client.get(f"/v1/projects/{lab.project}/assets", headers=lab.viewer).json()[
        "items"
    ][0]["asset_id"]
    live = lab.test_case("live", asset_id, freshness_reference="now")
    run = lab.start(lab.plan(live))
    drain(Worker(worker_engine, config(feed, tmp_path)))
    failing = lab.client.get(
        f"/v1/assurance/runs/{run['run_id']}/observations", params={"outcome": "fail"},
        headers=lab.viewer,
    ).json()["items"]  # fmt: skip
    assert [o["rule"] for o in failing] == ["freshness"]  # January data is stale in November


# ---- the API ----------------------------------------------------------------------------------


def test_idempotency_keys_return_the_same_run(lab: Lab) -> None:
    plan = lab.plan(lab.tests["batch_0"])
    first = lab.start(plan, **{"Idempotency-Key": "ci-1234"})
    again = lab.start(plan, **{"Idempotency-Key": "ci-1234"})
    assert again["run_id"] == first["run_id"]
    other = lab.client.post(
        "/v1/assurance/runs", json={"plan_id": plan, "trigger_ref": "abc"},
        headers=lab.engineer | {"Idempotency-Key": "ci-1234"},
    )  # fmt: skip
    assert (other.status_code, other.json()["code"]) == (422, "idempotency.key_reused")
    assert lab.start(plan)["run_id"] != first["run_id"]  # no key: a new run every time


def test_runs_api_roles_filters_and_isolation(lab: Lab, client: TestClient, owner: Engine) -> None:
    plan = lab.plan(lab.tests["batch_0"])
    run = lab.start(plan, trigger_ref="abc123")
    denied = client.post("/v1/assurance/runs", json={"plan_id": plan}, headers=lab.viewer)
    assert denied.status_code == 403
    missing = client.post("/v1/assurance/runs", json={"plan_id": str(uuid4())},
                          headers=lab.engineer)  # fmt: skip
    assert (missing.status_code, missing.json()["code"]) == (404, "plans.not_found")
    listed = client.get("/v1/assurance/runs", params={"project_id": lab.project, "state": "queued"},
                        headers=lab.viewer).json()["items"]  # fmt: skip
    assert run["run_id"] in {r["run_id"] for r in listed}
    assert listed[0]["run_id"] >= listed[-1]["run_id"]  # newest first
    later = client.get("/v1/assurance/runs", params={"since": "2999-01-01T00:00:00Z"},
                       headers=lab.viewer).json()  # fmt: skip
    assert later["items"] == []
    outsider = new_tenant(owner)
    headers = auth(login(client, outsider.admin_email), outsider.tenant_id)
    assert client.get(f"/v1/assurance/runs/{run['run_id']}", headers=headers).status_code == 404
    assert client.get(f"/v1/assurance/runs/{run['run_id']}/observations",
                      headers=headers).status_code == 404  # fmt: skip


def test_a_run_needs_a_published_contract(lab: Lab) -> None:
    asset = lab.post(f"/v1/projects/{lab.project}/assets", {"kind": "table", "name": "bare"})
    test = lab.test_case("bare", asset["asset_id"])
    response = lab.client.post("/v1/assurance/runs", json={"plan_id": lab.plan(test)},
                               headers=lab.engineer)  # fmt: skip
    assert (response.status_code, response.json()["code"]) == (422, "runs.no_contract")


# ---- the database guards ----------------------------------------------------------------------


def test_the_state_machine_and_pins_are_enforced(
    lab: Lab, owner: Engine, feed: Path, worker_engine: Engine, tmp_path: Path
) -> None:
    finished = lab.start(lab.plan(lab.tests["batch_0"]))["run_id"]
    drain(Worker(worker_engine, config(feed, tmp_path)))
    queued = lab.start(lab.plan(lab.tests["batch_0"]))["run_id"]
    statements = [
        ("UPDATE rai.runs SET state = 'passed', verdict = 'pass' WHERE run_id = :q", "cannot go"),
        ("UPDATE rai.runs SET pins = '{}'::jsonb, state = 'aborted' WHERE run_id = :q",
         "inputs are pinned"),
        ("UPDATE rai.runs SET state = 'aborted' WHERE run_id = :f", "is finished"),
        ("DELETE FROM rai.observations WHERE run_id = :f", "insert-only"),
    ]  # fmt: skip
    for statement, message in statements:
        with pytest.raises(DBAPIError, match=message), owner.begin() as conn:
            conn.execute(text(statement), {"q": queued, "f": finished})


def test_the_worker_role_sees_jobs_but_no_tenant_data(
    lab: Lab, worker_engine: Engine, owner: Engine
) -> None:
    lab.start(lab.plan(lab.tests["batch_0"]))
    with worker_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM rai.jobs")).scalar_one() >= 1
        for table in ("runs", "observations", "assurance_projects", "data_contracts"):
            assert conn.execute(text(f"SELECT count(*) FROM rai.{table}")).scalar_one() == 0
    Worker(worker_engine, WorkerConfig(name="x", state_dir=Path("."))).check()
    with pytest.raises(WorkerSetupError, match="must log in as rai_worker"):
        Worker(owner, WorkerConfig(name="x", state_dir=Path("."))).check()


def test_worker_command(
    lab: Lab, worker_url: str, feed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connectors = tmp_path / "connectors"
    connectors.mkdir()
    (connectors / "feed.yaml").write_text(
        f"name: feed\nenvironment: SANDBOX-01\ntype: csv\nroot: {(feed / 'feed').as_posix()}\n",
        encoding="utf-8",
    )
    run = lab.start(lab.plan(lab.tests["batch_0"]))
    monkeypatch.setenv("RAI_WORKER_DATABASE_URL", worker_url)
    monkeypatch.setenv("RAI_WORKER_CONNECTORS", str(connectors))
    monkeypatch.setenv("RAI_WORKER_STATE", str(tmp_path / "state"))
    runner = CliRunner()
    while "processed one job" in (result := runner.invoke(cli, ["worker", "--once"])).output:
        assert result.exit_code == 0, result.output
    assert "no job waiting" in result.output
    assert lab.run(run["run_id"])["state"] == "passed"

    monkeypatch.setenv("RAI_DATABASE_URL", DSN)
    monkeypatch.setenv("RAI_WORKER_DB_PASSWORD", WORKER_PASSWORD)
    assert runner.invoke(cli, ["db", "set-worker-password"]).exit_code == 0
    monkeypatch.delenv("RAI_WORKER_DATABASE_URL")
    missing = runner.invoke(cli, ["worker", "--once"])
    assert (missing.exit_code, "RAI_WORKER_DATABASE_URL" in missing.output) == (2, True)
