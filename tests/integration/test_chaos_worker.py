"""The worker's half of fault injection against a real database (B-24: S-03, S-05, R-03, D-02).

The worker preflight reads the run's environment and allow-list through RLS, signs,
and the request goes to the real agent code over HTTP (with fake Toxiproxy and
Docker backends; the real ones are exercised by the CI job "Chaos agent"). Every
outcome is recorded as an observation of the run.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from chaos_agent.agent import Agent
from chaos_agent.faults import StateFile
from chaos_agent.server import handler_for
from chaos_agent.signing import parse_keys
from rai.core.runs import Pins
from rai.core.tenancy import set_context
from rai.worker.chaos import AgentClient, ChaosClient, FaultOutcome, FaultSpec, Signer
from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab
from tests.unit.test_chaos_agent import FakeDocker, FakeToxiproxy

pytestmark = needs_db

ENV = "SANDBOX-01"  # the Lab's environment (ceiling 900 s)
PROXY_LABEL = "rai.target=toxiproxy"


class Bench:
    """A queued run with its pins, an allow-list, and an agent that trusts the worker."""

    def __init__(self, lab: Lab, client: TestClient, owner: Engine, worker: Engine, tmp: Path):
        self.lab, self.owner, self.worker = lab, owner, worker
        targets = f"/v1/environments/{lab.environment}/targets"
        for target in ({"kind": "proxy", "name": "ledger-db-proxy", "required_label": PROXY_LABEL},
                       {"kind": "container", "name": "payments-api",
                        "required_label": "rai.target=payments-api",
                        "image_digest": "sha256:" + "a" * 64},
                       {"kind": "db", "name": "ledger-db"}):  # fmt: skip
            response = client.post(targets, json=target, headers=lab.admin)
            assert response.status_code == 201, response.text
        plan = lab.post(f"/v1/projects/{lab.project}/plans",
                        {"name": "chaos-plan", "items": [{"test_id": lab.tests["batch_0"]}],
                         "environment_id": lab.environment})["plan_id"]  # fmt: skip
        self.run_id = UUID(lab.start(plan)["run_id"])
        with owner.connect() as conn:
            self.pins = Pins.model_validate(
                conn.execute(
                    text("SELECT pins FROM rai.runs WHERE run_id = :r"), {"r": self.run_id}
                ).scalar_one()
            )
        self.toxiproxy, self.docker = FakeToxiproxy(), FakeDocker()
        self.agent = Agent(StateFile(tmp / "state.json"), self.toxiproxy, self.docker,
                           "sandboxbank", environment=ENV)  # fmt: skip
        self.signer = Signer.generate()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(self.agent, parse_keys(self.signer.public_key))
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.chaos = ChaosClient(self.signer, AgentClient(self.url))

    def inject(self, step: str, chaos: ChaosClient | None = None, **spec: Any) -> FaultOutcome:
        fault = FaultSpec.model_validate(
            {
                "type": "connection_drop",
                "target": "ledger-db-proxy",
                "duration_seconds": 60,
                "blast_radius": ["ledger-db-proxy"],
            }
            | spec
        )
        with self.worker.begin() as conn:
            set_context(conn, self.lab.tenant.tenant_id, "worker", "w-chaos-test")
            return (chaos or self.chaos).inject(
                conn, self.pins, fault, run_id=self.run_id, tenant_id=self.lab.tenant.tenant_id,
                step_key=step,
            )  # fmt: skip

    def observations(self) -> dict[str, tuple[str, str, dict[str, Any]]]:
        with self.owner.connect() as conn:
            rows = conn.execute(
                text("SELECT step_key, rule, kind::text, outcome::text, observed_value "
                     "FROM rai.observations WHERE run_id = :r AND rule LIKE 'fault.%'"),
                {"r": self.run_id},
            ).all()  # fmt: skip
        return {f"{r.step_key}/{r.rule}": (r.kind, r.outcome, r.observed_value) for r in rows}


@pytest.fixture
def bench(
    lab: Lab, client: TestClient, owner: Engine, worker_engine: Engine, tmp_path: Path
) -> Iterator[Bench]:
    made = Bench(lab, client, owner, worker_engine, tmp_path)
    yield made
    made.server.shutdown()


def test_a_signed_fault_is_injected_and_its_onset_recorded_once(bench: Bench) -> None:
    """FR-07.1 through the worker; R-03: the onset is an observation of the run."""
    outcome = bench.inject("item-1.fault-1")
    assert outcome.status == "injected", outcome
    assert bench.toxiproxy.proxies["ledger-db-proxy"]["enabled"] is False
    fault = bench.agent.open_faults()[0]
    assert fault.signed_by == bench.signer.key_id
    # The label and the ceiling come from the registry, not from the test definition.
    assert fault.request.required_label == PROXY_LABEL
    assert fault.request.max_duration_seconds == 900
    assert fault.request.run_id == str(bench.run_id)

    # A worker that crashed after the agent acted retries the step: the agent refuses a
    # second fault (409) and the worker records the same onset, once.
    retried = bench.inject("item-1.fault-1")
    assert (retried.status, retried.onset) == ("injected", outcome.onset)
    assert len(bench.agent.state.load()) == 1
    recorded = bench.observations()
    assert list(recorded) == ["item-1.fault-1/fault.onset"]
    kind, result, observed = recorded["item-1.fault-1/fault.onset"]
    assert (kind, result) == ("metric", "pass")
    assert observed["onset"] == outcome.onset
    assert observed["signed_by"] == bench.signer.key_id


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        ({"target": "analytics-db-proxy", "blast_radius": ["analytics-db-proxy"]},
         "not on environment 'SANDBOX-01''s allow-list: analytics-db-proxy"),
        ({"blast_radius": ["ledger-db-proxy", "unknown"]}, "allow-list: unknown"),
        ({"duration_seconds": 901}, "exceeds environment 'SANDBOX-01''s ceiling of 900 s"),
        ({"type": "container_stop"}, "container_stop needs a container target"),
        ({"type": "latency", "latency_ms": 5, "target": "payments-api",
          "blast_radius": ["payments-api"]}, "latency needs a proxy target"),
        ({"type": "container_stop", "target": "ledger-db", "blast_radius": ["ledger-db"]},
         "'ledger-db' is a db"),
    ],
)  # fmt: skip
def test_the_worker_preflight_refuses_before_anything_is_signed(
    bench: Bench, spec: dict[str, Any], reason: str
) -> None:
    """S-05 and D-02, the worker's side: refused against the registry; the agent never
    hears of it, and the refusal is kept as an observation."""
    outcome = bench.inject("item-1.fault-1", **spec)
    assert (outcome.status, outcome.by) == ("refused", "worker")
    assert any(reason in r for r in outcome.reasons), outcome.reasons
    assert bench.agent.state.load() == {}
    kind, result, observed = bench.observations()["item-1.fault-1/fault.refused"]
    assert (kind, result, observed["by"]) == ("refused", "skipped", "worker")


def test_a_retired_environment_takes_no_faults(bench: Bench, client: TestClient) -> None:
    path = f"/v1/environments/{bench.lab.environment}"
    retired = client.patch(path, json={"status": "retired"},
                           headers=bench.lab.admin | {"If-Match": '"v1"'})  # fmt: skip
    assert retired.status_code == 200, retired.text
    outcome = bench.inject("item-1.fault-1")
    assert outcome.reasons == ("environment 'SANDBOX-01' is retired",)
    assert bench.agent.state.load() == {}


def test_the_agent_refuses_a_worker_it_does_not_trust(bench: Bench) -> None:
    """S-03: a request signed with any other key is refused by the agent (403), and the
    refusal is recorded with the agent's reason."""
    stranger = ChaosClient(Signer.generate(), AgentClient(bench.url))
    outcome = bench.inject("item-1.fault-1", chaos=stranger)
    assert (outcome.status, outcome.by) == ("refused", "agent")
    assert outcome.reasons == ("the request is signed with a key the agent does not trust",)
    assert bench.agent.state.load() == {}
    assert bench.observations()["item-1.fault-1/fault.refused"][2]["by"] == "agent"


def test_an_unreachable_agent_is_recorded_as_an_error(bench: Bench) -> None:
    bench.server.shutdown()
    bench.server.server_close()
    outcome = bench.inject("item-1.fault-1")
    assert outcome.status == "error"
    assert bench.observations()["item-1.fault-1/fault.error"][:2] == ("note", "error")
