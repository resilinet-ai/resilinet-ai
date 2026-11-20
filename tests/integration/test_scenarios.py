"""Chaos scenarios end to end against a real database (B-26: FR-07.1, FR-07.2, NFR-SAF-1;
threats T-05, R-02, E-03).

Publish and review through the API, start a run, and let the worker execute it
through the real agent code over HTTP (fake Toxiproxy and Docker; the real ones are
exercised by the CI job "Chaos agent").
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from chaos_agent.agent import Agent
from chaos_agent.faults import StateFile
from chaos_agent.server import handler_for
from chaos_agent.signing import parse_keys
from rai.worker.chaos import AgentClient, ChaosClient, Signer
from rai.worker.executor import Executor
from rai.worker.service import Worker
from tests.integration.apikit import needs_db, signed_in_member
from tests.integration.runkit import Lab, config, drain
from tests.integration.test_waivers import as_app
from tests.unit.test_chaos_agent import FakeDocker, FakeToxiproxy

pytestmark = needs_db

FAULTS = [
    {"type": "connection_drop", "target": "ledger-db-proxy", "duration_seconds": 1},
    {"type": "container_stop", "target": "payments-api", "duration_seconds": 1},
]
RADIUS = ["ledger-db-proxy", "payments-api"]


class Chaos:
    """A Lab with a reviewer, an allow-list, and an agent that trusts the worker's key."""

    def __init__(self, lab: Lab, client: TestClient, owner: Engine, tmp: Path) -> None:
        self.lab, self.client, self.owner = lab, client, owner
        self.reviewer = signed_in_member(client, owner, lab.tenant, "reviewer")
        targets = f"/v1/environments/{lab.environment}/targets"
        for target in ({"kind": "proxy", "name": "ledger-db-proxy",
                        "required_label": "rai.target=toxiproxy"},
                       {"kind": "container", "name": "payments-api",
                        "required_label": "rai.target=payments-api"}):  # fmt: skip
            assert client.post(targets, json=target, headers=lab.admin).status_code == 201
        self.toxiproxy, self.docker = FakeToxiproxy(), FakeDocker()
        self.agent = Agent(StateFile(tmp / "agent" / "state.json"), self.toxiproxy, self.docker,
                           "sandboxbank", environment="SANDBOX-01")  # fmt: skip
        self.signer = Signer.generate()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(self.agent, parse_keys(self.signer.public_key))
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def scenario(self, faults: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
        body = {"name": "ledger-outage", "blast_radius": RADIUS, "faults": faults or FAULTS}
        response = self.client.post(f"/v1/projects/{self.lab.project}/scenarios",
                                    json=body | extra, headers=self.lab.engineer)  # fmt: skip
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()
        return result

    def review(self, scenario_id: str, decision: str = "approve", **headers: str) -> Any:
        url = f"/v1/scenarios/{scenario_id}/approvals"
        return self.client.post(url, json={"decision": decision}, headers=headers or self.reviewer)

    def plan(self, scenario_id: str, environment: bool = True) -> str:
        item = {"item_type": "scenario", "scenario_id": scenario_id}
        body: dict[str, Any] = {"name": "chaos", "items": [item]}
        if environment:
            body["environment_id"] = self.lab.environment
        plan_id: str = self.lab.post(f"/v1/projects/{self.lab.project}/plans", body)["plan_id"]
        return plan_id

    def worker(self, worker_engine: Engine, feed: Path, tmp: Path, signer: Signer | None = None,
               **hooks: Any) -> Worker:  # fmt: skip
        chaos = ChaosClient(signer or self.signer, AgentClient(self.url))
        cfg = dataclasses.replace(config(feed, tmp), chaos=chaos)
        worker = Worker(worker_engine, cfg)
        worker.executor = Executor(worker_engine, cfg, renew=worker.renew, **hooks)
        return worker

    def observations(self, run_id: str) -> dict[str, tuple[str, str, dict[str, Any]]]:
        with self.owner.connect() as conn:
            rows = conn.execute(
                text("SELECT step_key, rule, kind::text, outcome::text, observed_value "
                     "FROM rai.observations WHERE run_id = :r"), {"r": run_id},
            ).all()  # fmt: skip
        return {f"{r.step_key}/{r.rule}": (r.kind, r.outcome, r.observed_value) for r in rows}


@pytest.fixture
def chaos(lab: Lab, client: TestClient, owner: Engine, tmp_path: Path) -> Iterator[Chaos]:
    made = Chaos(lab, client, owner, tmp_path)
    yield made
    made.server.shutdown()


# ---- publishing and review --------------------------------------------------------------------


def test_scenarios_are_versioned_and_checked(chaos: Chaos, client: TestClient) -> None:
    first = chaos.scenario(description="drop the ledger, then stop the API")
    assert (first["version"], first["status"]) == (1, "pending")
    assert chaos.scenario()["version"] == 2
    path = f"/v1/projects/{chaos.lab.project}/scenarios"
    outside = client.post(path, headers=chaos.lab.engineer, json={
        "name": "bad", "blast_radius": ["payments-api"], "faults": FAULTS})  # fmt: skip
    assert outside.status_code == 422
    assert "outside the declared blast radius" in outside.json()["detail"]
    latency = {"type": "latency", "target": "ledger-db-proxy", "duration_seconds": 5}
    no_latency = client.post(path, headers=chaos.lab.engineer, json={
        "name": "bad", "blast_radius": RADIUS, "faults": [latency]})  # fmt: skip
    assert no_latency.status_code == 422
    assert client.post(path, headers=chaos.lab.viewer, json={
        "name": "x", "blast_radius": RADIUS, "faults": FAULTS}).status_code == 403  # fmt: skip
    listed = client.get(path, params={"name": "ledger-outage"}, headers=chaos.lab.viewer)
    assert [s["version"] for s in listed.json()["items"]] == [1, 2]


def test_review_is_append_only_and_never_by_the_author(
    chaos: Chaos, client: TestClient, owner: Engine
) -> None:
    """R-02 and E-03: decisions are kept, the latest counts, and nobody reviews their own."""
    scenario = chaos.scenario()
    sid = scenario["scenario_id"]
    assert chaos.review(sid, **chaos.lab.engineer).status_code == 403  # engineers cannot review
    assert chaos.review(sid).status_code == 201
    assert chaos.review(sid, "reject").status_code == 201
    detail = client.get(f"/v1/scenarios/{sid}", headers=chaos.lab.viewer).json()
    assert detail["status"] == "rejected"
    assert [a["decision"] for a in detail["approvals"]] == ["approve", "reject"]
    assert chaos.review(sid, **chaos.lab.admin).status_code == 201  # admins may review too
    assert client.get(f"/v1/scenarios/{sid}", headers=chaos.lab.viewer).json()["status"] == (
        "approved"
    )

    with owner.begin() as conn:  # the author becomes a reviewer: still not their own
        conn.execute(text("UPDATE rai.memberships SET role = 'reviewer' WHERE user_id = :u"),
                     {"u": scenario["created_by"]})  # fmt: skip
    own = chaos.review(sid, **chaos.lab.engineer)
    assert (own.status_code, own.json()["code"]) == (403, "scenarios.self_review")
    with pytest.raises(DBAPIError, match="other than its author"), owner.begin() as conn:
        conn.execute(
            text("INSERT INTO rai.scenario_approvals (tenant_id, scenario_id, reviewer_id, "
                 "decision) VALUES (:t, :s, :u, 'approve')"),
            {"t": chaos.lab.tenant.tenant_id, "s": sid, "u": scenario["created_by"]},
        )  # fmt: skip
    for statement in ("UPDATE rai.chaos_scenarios SET description = 'x' WHERE scenario_id = :s",
                      "DELETE FROM rai.scenario_approvals WHERE scenario_id = :s"):  # fmt: skip
        with pytest.raises(DBAPIError), owner.begin() as conn:
            conn.execute(text(statement), {"s": sid})


def test_a_run_starts_only_with_an_approved_scenario_and_an_environment(chaos: Chaos) -> None:
    sid = chaos.scenario()["scenario_id"]
    pending = chaos.lab.client.post("/v1/assurance/runs", json={"plan_id": chaos.plan(sid)},
                                    headers=chaos.lab.engineer)  # fmt: skip
    assert (pending.status_code, pending.json()["code"]) == (409, "runs.scenario_not_approved")
    chaos.review(sid)
    nowhere = chaos.lab.client.post(
        "/v1/assurance/runs", json={"plan_id": chaos.plan(sid, environment=False)},
        headers=chaos.lab.engineer,
    )  # fmt: skip
    assert nowhere.json()["code"] == "runs.scenario_needs_environment"
    run = chaos.lab.start(chaos.plan(sid))
    pinned = chaos.lab.run(run["run_id"])
    assert pinned["state"] == "queued"


def lapse(chaos: Chaos, scenario_id: str, *, created: str, expires: str) -> None:
    """An approval written straight to the database with chosen times: through the API
    an approval cannot be dated in the past or expire within seconds."""
    with as_app(chaos.owner, chaos.lab) as conn:
        conn.execute(
            text("INSERT INTO rai.scenario_approvals (scenario_id, reviewer_id, decision, "
                 f"created_at, expires_at) VALUES (:s, :u, 'approve', {created}, {expires})"),
            {"s": scenario_id, "u": chaos.lab.tenant.admin_id},
        )  # fmt: skip


def test_an_approval_expires_and_must_be_given_again(chaos: Chaos) -> None:
    """B-43 (FR-07.2, NFR-SAF-1): 30 days by default, at most 90; then nothing runs."""
    sid = chaos.scenario()["scenario_id"]
    soon = datetime.now(UTC) + timedelta(days=91)
    for body, code in [
        ({"decision": "approve", "expires_at": soon.isoformat()}, "scenarios.expiry_out_of_range"),
        ({"decision": "approve", "expires_at": "2026-12-01T00:00:00"},
         "scenarios.expiry_without_zone"),
        ({"decision": "reject", "expires_at": soon.isoformat()}, "scenarios.expiry_on_reject"),
    ]:  # fmt: skip
        refused = chaos.client.post(f"/v1/scenarios/{sid}/approvals", json=body,
                                    headers=chaos.reviewer)  # fmt: skip
        assert (refused.status_code, refused.json()["code"]) == (422, code), body
    lapse(chaos, sid, created="clock_timestamp() - interval '10 days'",
          expires="clock_timestamp() - interval '1 day'")  # fmt: skip
    detail = chaos.client.get(f"/v1/scenarios/{sid}", headers=chaos.lab.viewer).json()
    assert (detail["status"], detail["approvals"][-1]["expired"]) == ("expired", True)
    late = chaos.client.post("/v1/assurance/runs", json={"plan_id": chaos.plan(sid)},
                             headers=chaos.lab.engineer)  # fmt: skip
    assert (late.status_code, late.json()["code"]) == (409, "runs.scenario_approval_expired")

    with (
        pytest.raises(DBAPIError, match="scenario_approvals_expiry"),
        as_app(chaos.owner, chaos.lab) as conn,
    ):  # an approval without an expiry cannot be written any more
        conn.execute(
            text("INSERT INTO rai.scenario_approvals (scenario_id, reviewer_id, decision) "
                 "VALUES (:s, :u, 'approve')"), {"s": sid, "u": chaos.lab.tenant.admin_id},
        )  # fmt: skip
    approved = chaos.review(sid).json()  # a fresh review makes it runnable again
    term = datetime.fromisoformat(approved["expires_at"]) - datetime.now(UTC)
    assert timedelta(days=29, hours=23) < term <= timedelta(days=30)  # the default term
    assert approved["expired"] is False
    assert chaos.lab.start(chaos.plan(sid))["state"] == "queued"


def test_the_worker_refuses_a_scenario_whose_approval_expired_after_queueing(
    chaos: Chaos, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))  # leftovers: refused
    sid = chaos.scenario()["scenario_id"]
    chaos.review(sid)
    run = chaos.lab.start(chaos.plan(sid))
    lapse(chaos, sid, created="clock_timestamp()",
          expires="clock_timestamp() + interval '1 second'")  # fmt: skip
    time.sleep(1.2)
    assert chaos.worker(worker_engine, feed, tmp_path).run_once()
    refused = chaos.lab.run(run["run_id"])
    assert refused["state"] == "refused"
    assert "approval expired at" in refused["reason"]
    assert chaos.agent.state.load() == {}  # nothing reached the agent


@pytest.mark.parametrize(
    ("item", "code"),
    [
        ({"item_type": "scenario"}, "plans.scenario_required"),
        ({"item_type": "scenario", "scenario_id": "00000000-0000-7000-8000-000000000000"},
         "plans.unknown_scenario"),
        ({"item_type": "test_case", "scenario_id": "00000000-0000-7000-8000-000000000000"},
         "plans.test_required"),
    ],
)  # fmt: skip
def test_plan_items_are_checked(chaos: Chaos, item: dict[str, Any], code: str) -> None:
    response = chaos.lab.client.post(f"/v1/projects/{chaos.lab.project}/plans",
                                     json={"name": "p", "items": [item]},
                                     headers=chaos.lab.engineer)  # fmt: skip
    assert (response.status_code, response.json()["code"]) == (422, code)


# ---- execution -----------------------------------------------------------------------------


def test_the_worker_runs_an_approved_scenario(
    chaos: Chaos, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    """FR-07.1 through a run: each fault is signed, injected, held and reverted in order,
    and its onset and revert are observations of the run."""
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))  # leftovers: refused
    sid = chaos.scenario()["scenario_id"]
    chaos.review(sid)
    run = chaos.lab.start(chaos.plan(sid))
    assert chaos.worker(worker_engine, feed, tmp_path).run_once()
    done = chaos.lab.run(run["run_id"])
    assert (done["state"], done["verdict"]) == ("passed", "pass")
    assert [(s["step_key"], s["state"]) for s in done["steps"]] == [("item-1", "done")]
    seen = chaos.observations(run["run_id"])
    assert sorted(seen) == [
        "item-1.fault-1/fault.onset", "item-1.fault-1/fault.reverted",
        "item-1.fault-2/fault.onset", "item-1.fault-2/fault.reverted",
    ]  # fmt: skip
    assert seen["item-1.fault-1/fault.reverted"][2]["reverted_by"] == "request"
    assert chaos.toxiproxy.calls == [("set_enabled", "ledger-db-proxy", "False"),
                                     ("set_enabled", "ledger-db-proxy", "True")]  # fmt: skip
    assert chaos.docker.calls == [("stop", "c1"), ("start", "c1")]  # in order, one at a time
    faults = chaos.agent.state.load().values()
    assert {f.request.step_key for f in faults} == {"item-1.fault-1", "item-1.fault-2"}
    assert all(f.request.run_id == run["run_id"] for f in faults)
    assert {f.request.scenario for f in faults} == {f"{sid}@1"}  # B-27: signed identity
    assert all(f.request.approval_expiry for f in faults)  # B-43: signed with its expiry


def test_a_retry_after_a_crash_injects_nothing_twice(
    chaos: Chaos, worker_engine: Engine, owner: Engine, feed: Path, tmp_path: Path
) -> None:
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))  # leftovers: refused
    sid = chaos.scenario()["scenario_id"]
    chaos.review(sid)
    run = chaos.lab.start(chaos.plan(sid))

    def die(step: str) -> None:
        raise SystemExit("worker killed before the step was recorded")

    with pytest.raises(SystemExit):
        chaos.worker(worker_engine, feed, tmp_path / "a", before_commit=die).run_once()
    with owner.begin() as conn:
        conn.execute(
            text("UPDATE rai.jobs SET lease_expires_at = clock_timestamp() "
                 "- interval '1 second' WHERE run_id = :r"),
            {"r": run["run_id"]},
        )  # fmt: skip
    assert chaos.worker(worker_engine, feed, tmp_path / "b").run_once()
    done = chaos.lab.run(run["run_id"])
    assert (done["state"], done["verdict"]) == ("passed", "pass")
    assert len(chaos.agent.state.load()) == 2  # the agent's replay guard: no second fault
    assert chaos.docker.calls.count(("stop", "c1")) == 1
    assert len(chaos.observations(run["run_id"])) == 4  # recorded once each


def test_the_worker_refuses_what_it_cannot_run_safely(
    chaos: Chaos, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))  # leftovers: refused
    sid = chaos.scenario()["scenario_id"]
    chaos.review(sid)
    no_keys = chaos.lab.start(chaos.plan(sid))
    plain = Worker(worker_engine, config(feed, tmp_path / "plain"))  # no signing key
    assert plain.run_once()
    refused = chaos.lab.run(no_keys["run_id"])
    assert refused["state"] == "refused"
    assert "not configured for faults" in refused["reason"]

    withdrawn = chaos.lab.start(chaos.plan(sid))
    chaos.review(sid, "reject")
    assert chaos.worker(worker_engine, feed, tmp_path).run_once()
    assert "approval was withdrawn" in chaos.lab.run(withdrawn["run_id"])["reason"]
    assert chaos.agent.state.load() == {}  # nothing reached the agent


def test_a_fault_the_agent_refuses_ends_the_step_in_error(
    chaos: Chaos, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))  # leftovers: refused
    sid = chaos.scenario()["scenario_id"]
    chaos.review(sid)
    run = chaos.lab.start(chaos.plan(sid))
    stranger = chaos.worker(worker_engine, feed, tmp_path, signer=Signer.generate())
    assert stranger.run_once()
    done = chaos.lab.run(run["run_id"])
    assert (done["state"], done["verdict"]) == ("failed", "fail")
    step = done["steps"][0]
    assert step["state"] == "error"
    assert "refused by the agent" in step["error"]
    seen = chaos.observations(run["run_id"])
    assert list(seen) == ["item-1.fault-1/fault.refused"]  # the second fault was not tried
    assert chaos.agent.state.load() == {}
