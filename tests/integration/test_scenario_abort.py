"""A fault cut short ends its run ``aborted`` (FR-07.3, NFR-SAF-1).

The kill switch, or an agent restart, ends a fault before its declared time. The
scenario then did not run as reviewed, so the run must not pass: the worker notices
while it holds the fault, records the step in ``error`` and ends the run ``aborted``,
with no verdict, no recovery measurement and no later fault. Real agent code over
HTTP, fake Toxiproxy and Docker, as in ``test_scenarios``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from rai.worker.chaos import AgentClient
from rai.worker.service import Worker
from tests.integration.apikit import needs_db
from tests.integration.runkit import config, drain
from tests.integration.test_scenarios import RADIUS, Chaos, chaos

pytestmark = needs_db
__all__ = ["chaos"]  # the fixture, shared with the scenario tests

LONG = [  # long enough that only an early end lets the test finish quickly
    {"type": "connection_drop", "target": "ledger-db-proxy", "duration_seconds": 600},
    {"type": "container_stop", "target": "payments-api", "duration_seconds": 600},
]


def end_early(chaos: Chaos, by: str) -> Callable[[float], None]:
    """A sleep hook: the first time the worker waits on a fault, it is ended ``by``."""
    done: list[bool] = []

    def sleep(_: float) -> None:
        if not done:
            done.append(True)
            assert chaos.agent.revert_all(by=by)

    return sleep


def start(chaos: Chaos, worker_engine: Engine, feed: Path, tmp_path: Path) -> str:
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))  # leftovers: refused
    sid = chaos.scenario(LONG, blast_radius=RADIUS)["scenario_id"]
    chaos.review(sid)
    run_id: str = chaos.lab.start(chaos.plan(sid))["run_id"]
    return run_id


@pytest.mark.parametrize(
    ("by", "said"),
    [("kill", "stopped by the kill switch"), ("startup", "the chaos agent restarted")],
)
def test_a_fault_cut_short_aborts_the_run(
    chaos: Chaos, worker_engine: Engine, owner: Engine, feed: Path, tmp_path: Path,
    by: str, said: str,
) -> None:  # fmt: skip
    run_id = start(chaos, worker_engine, feed, tmp_path)
    worker = chaos.worker(worker_engine, feed, tmp_path, sleep=end_early(chaos, by))
    began = time.monotonic()
    assert worker.run_once()
    assert time.monotonic() - began < 30  # did not wait out the 600 s fault

    done = chaos.lab.run(run_id)
    assert (done["state"], done["verdict"]) == ("aborted", None)
    assert done["reason"].startswith("aborted: fault 1 (connection_drop on ledger-db-proxy)")
    assert said in done["reason"]
    [step] = done["steps"]
    assert (step["state"], step["verdict"]) == ("error", None)
    assert step["error"] == done["reason"]
    seen = chaos.observations(run_id)
    assert seen["item-1.fault-1/fault.reverted"][2]["reverted_by"] == by
    assert "item-1.fault-2/fault.onset" not in seen  # no later fault
    assert chaos.docker.calls == []  # payments-api was never stopped
    with owner.connect() as conn:
        measured = conn.execute(
            text("SELECT count(*) FROM rai.recovery_measurements WHERE run_id = :r"),
            {"r": run_id},
        ).scalar_one()
    assert measured == 0


def test_an_aborted_run_is_finished_for_every_later_worker(
    chaos: Chaos, worker_engine: Engine, owner: Engine, feed: Path, tmp_path: Path
) -> None:
    run_id = start(chaos, worker_engine, feed, tmp_path)
    chaos.worker(worker_engine, feed, tmp_path / "a", sleep=end_early(chaos, "kill")).run_once()
    with owner.begin() as conn:  # as if the job had been left behind
        conn.execute(
            text("UPDATE rai.jobs SET finished_at = NULL, lease_expires_at = "
                 "clock_timestamp() - interval '1 second' WHERE run_id = :r"),
            {"r": run_id},
        )  # fmt: skip
    chaos.worker(worker_engine, feed, tmp_path / "b").run_once()
    assert chaos.lab.run(run_id)["state"] == "aborted"
    assert len(chaos.agent.state.load()) == 1  # nothing injected again


def test_ordinary_endings_are_not_aborts(
    chaos: Chaos, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    """The agent's own expiry ends a fault at its declared time: the run is judged."""
    drain(Worker(worker_engine, config(feed, tmp_path / "earlier")))
    one = [{"type": "connection_drop", "target": "ledger-db-proxy", "duration_seconds": 1}]
    sid = chaos.scenario(one, blast_radius=RADIUS)["scenario_id"]
    chaos.review(sid)
    run_id = chaos.lab.start(chaos.plan(sid))["run_id"]

    def expire(seconds: float) -> None:
        time.sleep(seconds)
        chaos.agent.revert_expired()

    assert chaos.worker(worker_engine, feed, tmp_path, sleep=expire).run_once()
    done = chaos.lab.run(run_id)
    assert (done["state"], done["verdict"]) == ("passed", "pass")
    assert chaos.observations(run_id)["item-1.fault-1/fault.reverted"][2]["reverted_by"] in (
        "expiry", "request",
    )  # fmt: skip


def test_an_unreachable_agent_is_waited_out_not_taken_as_an_end() -> None:
    assert AgentClient("http://127.0.0.1:9", timeout=1).open_fault_ids() is None
