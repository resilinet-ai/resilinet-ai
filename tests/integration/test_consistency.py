"""Post-recovery consistency checks run by the worker (FR-08.3).

A ledger and a warehouse fact table, each read through its own worker connector
(CSV here; PostgreSQL in a deployment). The worker's ``sleep`` hook stands in for
the ETL catching up between rounds.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from rai.worker.executor import Executor
from rai.worker.service import Worker
from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab, config, drain
from tests.integration.test_lineage import asset, declare, lineage
from tests.integration.test_scenarios import Chaos

pytestmark = needs_db

LEDGER = """seq,txn_id,amount,status
1,a,10.00,posted
2,b,20.00,posted
3,c,5.00,declined
4,d,7.25,posted
"""
HEADER = "ledger_seq,txn_id,amount\n"
FACTS = HEADER + "1,a,10.00\n2,b,20.00\n4,d,7.25\n"


@dataclasses.dataclass
class Data:
    ledger: Path
    warehouse: Path

    def facts(self, text: str) -> None:
        (self.warehouse / "fact.csv").write_text(text, encoding="utf-8")

    def connectors(self) -> dict[str, Any]:
        return {
            name: {"name": name, "environment": "SANDBOX-01", "type": "csv",
                   "root": root.as_posix()}
            for name, root in (("ledger", self.ledger), ("warehouse", self.warehouse))
        }  # fmt: skip


@pytest.fixture
def data(tmp_path: Path) -> Data:
    made = Data(tmp_path / "ledger", tmp_path / "warehouse")
    for root in (made.ledger, made.warehouse):
        root.mkdir()
    (made.ledger / "transactions.csv").write_text(LEDGER, encoding="utf-8")
    made.facts(FACTS)
    return made


def check(lab: Lab, *, upstream: bool = True, **definition: Any) -> str:
    """Assets, the declared hop ledger -> facts, and a consistency_check test case."""
    source = asset(lab, "table", "ledger_transactions", "transactions.csv")
    target = asset(lab, "table", "fact_transaction", "fact.csv")
    if upstream:
        declare(lab, target, source, "etl-job@1")
    body = {
        "name": "ledger.consistency", "type": "consistency_check", "target_asset_id": target,
        "definition": {
            "connector": "warehouse", "environment": "SANDBOX-01",
            "source": {"asset": "ledger_transactions", "connector": "ledger",
                       "where": {"status": "posted"}, "rename": {"seq": "ledger_seq"}},
            "key": ["txn_id"], "compare": ["amount", "ledger_seq"],
            "within_seconds": 60, "interval_seconds": 0.01,
        } | definition,
    }  # fmt: skip
    test_id: str = lab.post(f"/v1/projects/{lab.project}/test-cases", body)["test_id"]
    return test_id


def worker(engine: Engine, feed: Path, tmp: Path, data: Data,
           sleep: Callable[[float], None] = lambda _: None, **extra: Any) -> Worker:  # fmt: skip
    cfg = config(feed, tmp, **(data.connectors() | extra))
    made = Worker(engine, cfg)
    made.executor = Executor(engine, cfg, renew=made.renew, sleep=sleep)
    return made


def plan(lab: Lab, *test_ids: str) -> str:
    body = {"name": "consistency", "environment_id": lab.environment,
            "items": [{"test_id": t} for t in test_ids]}  # fmt: skip
    plan_id: str = lab.post(f"/v1/projects/{lab.project}/plans", body)["plan_id"]
    return plan_id


def observed(lab: Lab, run_id: str) -> dict[str, dict[str, Any]]:
    response = lab.client.get(f"/v1/assurance/runs/{run_id}/observations", headers=lab.viewer)
    assert response.status_code == 200, response.text
    return {o["rule"]: o for o in response.json()["items"]}


def test_the_check_waits_for_the_target_to_catch_up(
    lab: Lab, data: Data, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    """FR-08.3: the warehouse is one posted row behind; the ETL loads it during the
    check; the check passes on the second round and records the hop's digests."""
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))
    data.facts(HEADER + "1,a,10.00\n2,b,20.00\n")
    run = lab.start(plan(lab, check(lab)))
    caught_up = lambda _: data.facts(FACTS)  # noqa: E731 - the ETL, between rounds
    assert worker(worker_engine, feed, tmp_path, data, sleep=caught_up).run_once()
    done = lab.run(run["run_id"])
    assert (done["state"], done["verdict"]) == ("passed", "pass"), done
    seen = observed(lab, run["run_id"])
    assert sorted(seen) == [
        "consistency.duplicated", "consistency.mismatched", "consistency.missing",
        "consistency.unexpected",
    ]  # fmt: skip
    assert all(o["outcome"] == "pass" for o in seen.values())
    assert seen["consistency.missing"]["observed_value"]["rounds"] == 2
    hops = lineage(lab, run["run_id"])["edges"]
    assert {h["from_name"] for h in hops} == {"ledger_transactions", "fact_transaction"}
    assert all(h["from_digest"] for h in hops)


def test_lost_doubled_and_changed_rows_fail_the_run(
    lab: Lab, data: Data, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))
    data.facts(HEADER + "2,b,20.00\n2,b,20.00\n4,d,9.99\n9,z,1.00\n")
    run = lab.start(plan(lab, check(lab, within_seconds=0)))
    assert worker(worker_engine, feed, tmp_path, data).run_once()
    done = lab.run(run["run_id"])
    assert (done["state"], done["verdict"]) == ("failed", "fail")
    step = done["steps"][0]
    assert (step["state"], step["verdict"]) == ("done", "fail")
    assert step["error"].startswith("not consistent after 1 round(s) in ")
    assert step["error"].endswith(": 1 missing, 1 duplicated, 1 unexpected, 1 mismatched")
    seen = observed(lab, run["run_id"])
    assert seen["consistency.missing"]["observed_value"]["sample"] == [{"txn_id": "a"}]
    assert seen["consistency.mismatched"]["observed_value"]["sample"] == [{"txn_id": "d"}]
    assert seen["consistency.unexpected"]["failing_count"] == 1


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"upstream": False}, "'ledger_transactions' is not upstream of 'fact_transaction'"),
        ({"connector": "nowhere"}, "connector 'nowhere' is not on this worker"),
        ({"key": []}, "the consistency check's definition is not valid (key:"),
        ({"environment": "STAGING-09"}, "environment 'STAGING-09' is not registered"),
    ],
)  # fmt: skip
def test_preflight_refuses_what_it_cannot_reconcile(
    lab: Lab, data: Data, worker_engine: Engine, feed: Path, tmp_path: Path,
    setup: dict[str, Any], reason: str,
) -> None:  # fmt: skip
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))
    run = lab.start(plan(lab, check(lab, **setup)))
    assert worker(worker_engine, feed, tmp_path, data).run_once()
    done = lab.run(run["run_id"])
    assert done["state"] == "refused"
    assert reason in done["reason"], done["reason"]


def test_a_check_that_cannot_read_ends_in_error(
    lab: Lab, data: Data, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))
    (data.warehouse / "fact.csv").unlink()
    run = lab.start(plan(lab, check(lab)))
    assert worker(worker_engine, feed, tmp_path, data).run_once()
    step = lab.run(run["run_id"])["steps"][0]
    assert step["state"] == "error"
    assert step["error"].startswith("could not reconcile:")


def test_after_a_scenario_the_check_runs_once_the_system_has_recovered(
    lab: Lab, client: TestClient, owner: Engine, data: Data, worker_engine: Engine,
    feed: Path, tmp_path: Path,
) -> None:  # fmt: skip
    """The plan the protocol uses: a chaos scenario, then the consistency check."""
    chaos = Chaos(lab, client, owner, tmp_path)
    try:
        drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))
        faults = [{"type": "connection_drop", "target": "ledger-db-proxy", "duration_seconds": 1}]
        sid = chaos.scenario(faults, blast_radius=["ledger-db-proxy"])["scenario_id"]
        chaos.review(sid)
        items = [{"item_type": "scenario", "scenario_id": sid}, {"test_id": check(lab)}]
        plan_id = lab.post(f"/v1/projects/{lab.project}/plans",
                           {"name": "outage-then-check", "environment_id": lab.environment,
                            "items": items})["plan_id"]  # fmt: skip
        run = lab.start(plan_id)
        made = chaos.worker(worker_engine, feed, tmp_path)
        made.executor.config = dataclasses.replace(
            made.executor.config, connectors=made.executor.config.connectors | data.connectors()
        )
        assert made.run_once()
        done = lab.run(run["run_id"])
        assert (done["state"], done["verdict"]) == ("passed", "pass"), done
        assert [s["step_key"] for s in sorted(done["steps"], key=lambda s: s["step_key"])] == [
            "item-1", "item-2"
        ]  # fmt: skip
    finally:
        chaos.server.shutdown()
