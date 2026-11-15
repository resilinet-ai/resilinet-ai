"""Helpers for run and worker integration tests (B-18, B-19): a tenant with a project,
batch assets with contracts and test cases, and a worker configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from rai.worker.executor import WorkerConfig
from rai.worker.service import Worker
from tests.integration.apikit import Tenant, auth, login, new_tenant, signed_in_member

ROOT = Path(__file__).resolve().parents[2]
TXN_YAML = (ROOT / "contracts" / "sandboxbank" / "transactions_raw.yaml").read_text("utf-8")
RULES = 36  # rules in transactions_raw@1


def config(
    feed: Path, state: Path, name: str | None = None, **extra: dict[str, Any]
) -> WorkerConfig:
    connector = {"name": "feed", "environment": "SANDBOX-01", "type": "csv",
                 "root": (feed / "feed").as_posix()}  # fmt: skip
    return WorkerConfig(name=name or f"w-{uuid4().hex[:6]}", state_dir=state,
                        connectors={"feed": connector} | extra)  # fmt: skip


class Lab:
    """A tenant with a project, two batch assets with contracts, and test cases."""

    def __init__(self, client: TestClient, owner: Engine) -> None:
        self.client = client
        self.tenant: Tenant = new_tenant(owner)
        self.engineer = signed_in_member(client, owner, self.tenant, "engineer")
        self.viewer = signed_in_member(client, owner, self.tenant, "viewer")
        self.admin = auth(login(client, self.tenant.admin_email), self.tenant.tenant_id)
        # B-22: tests run only in a registered environment (deny by default).
        self.environment = self.post("/v1/environments", {"name": "SANDBOX-01", "type": "SANDBOX"},
                                     self.admin)["environment_id"]  # fmt: skip
        self.project = self.post("/v1/projects", {"name": f"p-{uuid4().hex[:6]}"})["project_id"]
        self.tests: dict[str, str] = {}
        self.assets: dict[str, str] = {}
        for name, locator in (("batch_0", "transactions_raw/B000000.csv"),
                              ("batch_1", "transactions_raw/B000001.csv"),
                              ("missing", "transactions_raw/B999999.csv")):  # fmt: skip
            asset = self.post(f"/v1/projects/{self.project}/assets",
                              {"kind": "table", "name": name, "locator": locator})  # fmt: skip
            self.post(f"/v1/assets/{asset['asset_id']}/contracts", {"source": TXN_YAML})
            self.assets[name] = asset["asset_id"]
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
