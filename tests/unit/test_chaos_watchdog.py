"""The independent watchdog and the shared state lock (B-25: threat D-01; FR-07.3).

The watchdog against the real Docker and Toxiproxy (with the agent killed) and the
kill-switch drill are in scripts/chaos_e2e.py (CI job "Chaos agent").
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from chaos_agent.agent import Agent
from chaos_agent.clients import BackendError, HttpDocker
from chaos_agent.faults import Fault, LockTimeout, StateFile, now, stamp
from chaos_agent.server import main
from chaos_agent.watchdog import Watchdog, deadline, healthy, settings
from tests.unit.test_chaos_agent import LABEL, FakeDocker, FakeToxiproxy, backend, request

__all__ = ["backend"]  # the recording HTTP server fixture, shared with the agent tests

LATER = timedelta(minutes=10)


@pytest.fixture
def parts(tmp_path: Path) -> tuple[Agent, Watchdog]:
    agent = Agent(StateFile(tmp_path / "state.json"), FakeToxiproxy(), FakeDocker(), "sandboxbank")
    return agent, Watchdog(agent, grace_seconds=30, lock_wait=2)


def test_the_watchdog_reverts_only_overdue_faults(parts: Any) -> None:
    agent, dog = parts
    fault = agent.inject(request("container_stop", duration_seconds=60))
    assert dog.sweep(now()) == []  # within its duration
    due = deadline(fault, dog.grace)
    assert dog.sweep(due - timedelta(seconds=1)) == []  # past expiry, still in the grace
    reverted = dog.sweep(due)
    assert [f.fault_id for f in reverted] == [fault.fault_id]
    record = agent.state.load()[fault.fault_id]
    assert (record.status, record.reverted_by) == ("reverted", "watchdog")
    assert agent.docker.calls[-1] == ("start", "c1")
    assert dog.sweep(due + LATER) == []  # nothing left to do


def test_a_fault_left_mid_injection_is_reverted(parts: Any) -> None:
    """The agent died between recording a fault and finishing it: no expiry was set. The
    watchdog's deadline counts from the request, so it is reverted all the same."""
    agent, dog = parts
    original = agent.docker.action

    def die(container_id: str, verb: str) -> None:
        original(container_id, verb)
        raise SystemExit("agent killed mid-injection")

    agent.docker.action = die
    with pytest.raises(SystemExit):
        agent.inject(request("stale_upstream"))
    agent.docker.action = original
    (left,) = agent.state.load().values()
    assert (left.status, left.expires_at) == ("injecting", None)
    assert [f.fault_id for f in dog.sweep(now() + LATER)] == [left.fault_id]
    assert agent.docker.calls == [("pause", "c1"), ("unpause", "c1")]


def test_a_toxic_is_named_before_it_is_added(parts: Any) -> None:
    """Regression: the toxic's name was stored only after Toxiproxy accepted it, so a
    crash in between left a toxic no revert could find."""
    agent, dog = parts
    seen: list[str | None] = []
    original = agent.toxiproxy.add_toxic

    def crash(proxy: str, toxic: dict[str, Any]) -> None:
        seen.extend(f.detail.get("toxic") for f in agent.state.load().values())
        original(proxy, toxic)
        raise SystemExit("agent killed")

    agent.toxiproxy.add_toxic = crash
    with pytest.raises(SystemExit):
        agent.inject(request("latency"))
    assert seen[0] is not None
    assert seen[0].startswith("rai-")
    assert agent.toxiproxy.proxies["ledger-db-proxy"]["toxics"]
    dog.sweep(now() + LATER)
    assert agent.toxiproxy.proxies["ledger-db-proxy"]["toxics"] == {}


def test_the_watchdog_never_undoes_an_injection_in_progress(parts: Any) -> None:
    """The agent holds the state lock for the whole injection; the watchdog waits for it
    and reverts afterwards, never in between."""
    agent, dog = parts
    original = agent.docker.action
    swept: list[list[Any]] = []

    def slow(container_id: str, verb: str) -> None:
        sweeper = threading.Thread(target=lambda: swept.append(dog.sweep(now() + LATER)))
        sweeper.start()
        sweeper.join(timeout=0.3)
        assert sweeper.is_alive()  # blocked on the lock while the fault is applied
        original(container_id, verb)
        threading.Thread(target=sweeper.join).start()

    agent.docker.action = slow
    agent.inject(request("container_stop"))
    for _ in range(50):
        if swept:
            break
        threading.Event().wait(0.1)
    assert len(swept[0]) == 1
    assert agent.docker.calls == [("stop", "c1"), ("start", "c1")]


HOLD = """
import sys, time
from pathlib import Path
from chaos_agent.faults import StateFile
with StateFile(Path(sys.argv[1])).locked():
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def hold_lock(path: Path, seconds: float) -> subprocess.Popen[str]:
    """Another process (as the agent's container is) holding the state lock."""
    holder = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", HOLD, str(path), str(seconds)], stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    return holder


def test_the_state_lock_holds_across_processes(tmp_path: Path) -> None:
    state = StateFile(tmp_path / "state.json")
    holder = hold_lock(state.path, 1.5)
    with pytest.raises(LockTimeout), state.locked(timeout=0.3):
        pass
    holder.wait(timeout=10)
    with state.locked(timeout=2), state.locked():  # free again; re-entrant in one thread
        state.put(Fault(request=request("container_stop")))
    assert len(state.load()) == 1


def test_a_hung_agent_does_not_stop_the_watchdog(parts: Any) -> None:
    """The lock is held far longer than any injection takes: the watchdog reverts the
    overdue fault without it (idempotent) and leaves the record to the agent."""
    agent, _ = parts
    fault = agent.inject(request("container_stop"))
    holder = hold_lock(agent.state.path, 5)
    try:
        impatient = Watchdog(agent, grace_seconds=0, lock_wait=0.3)
        assert [f.fault_id for f in impatient.sweep(now() + LATER)] == [fault.fault_id]
        assert agent.docker.calls[-1] == ("start", "c1")
    finally:
        holder.kill()
        holder.wait(timeout=10)
    assert agent.state.load()[fault.fault_id].status == "active"  # untouched without the lock
    assert agent.revert_expired() == []  # not expired by its own clock yet
    assert agent.revert(fault.fault_id).reverted_by == "request"


def test_a_failed_revert_is_tried_again(parts: Any) -> None:
    agent, dog = parts
    fault = agent.inject(request("connection_drop"))
    original = agent.toxiproxy.set_enabled

    def down(proxy: str, enabled: bool) -> None:
        raise BackendError("toxiproxy down")

    agent.toxiproxy.set_enabled = down
    assert dog.sweep(now() + LATER) == []
    assert agent.state.load()[fault.fault_id].status == "active"
    agent.toxiproxy.set_enabled = original
    assert len(dog.sweep(now() + LATER)) == 1


def test_every_revert_is_recorded_with_who_did_it(parts: Any) -> None:
    agent, _ = parts
    one = agent.inject(request("container_stop"))
    two = agent.inject(request("connection_drop"))
    agent.revert(one.fault_id)
    agent.revert_all()
    faults = agent.state.load()
    assert (faults[one.fault_id].reverted_by, faults[two.fault_id].reverted_by) == (
        "request",
        "kill",
    )
    three = agent.inject(request("stale_upstream"))
    three.expires_at = stamp(now() - timedelta(seconds=1))
    agent.state.put(three)
    assert agent.revert_expired()[0].reverted_by == "expiry"


def test_reverts_are_idempotent_against_docker(backend: Any) -> None:
    """The agent and the watchdog may both revert one fault: unpausing a running
    container and reconnecting a connected one succeed."""
    url, _, answers = backend
    docker = HttpDocker(url)
    answers["POST /v1.41/containers/c1/unpause"] = (409, {"message": "Container c1 is not paused"})
    answers["POST /v1.41/networks/n/connect"] = (
        403, {"message": "endpoint with name ledger-db already exists in network n"},
    )  # fmt: skip
    docker.action("c1", "unpause")
    docker.connect("n", "c1", ["ledger-db"])
    answers["POST /v1.41/containers/c1/pause"] = (409, {"message": "Container c1 is not running"})
    with pytest.raises(BackendError):
        docker.action("c1", "pause")  # only reverts are forgiven
    answers["POST /v1.41/networks/n/connect"] = (404, {"message": "no such network"})
    with pytest.raises(BackendError):
        docker.connect("n", "c1", [])


def test_the_watchdog_runs_reports_health_and_stops(parts: Any, tmp_path: Path) -> None:
    _, dog = parts
    assert not healthy(tmp_path)
    stop = threading.Event()
    stop.set()
    dog.run(stop, interval=0.01)  # one sweep, then stops
    assert healthy(tmp_path)
    assert not healthy(tmp_path, max_age=timedelta(seconds=-1))
    assert settings({}) == (30, 20.0, 5.0)
    assert settings({"RAI_WATCHDOG_GRACE_SECONDS": "3", "RAI_WATCHDOG_INTERVAL": "1"})[::2] == (
        3,
        1.0,
    )


def test_the_command_line(
    parts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent, dog = parts
    fault = agent.inject(request("container_stop", required_label=LABEL))
    agent.revert(fault.fault_id)
    monkeypatch.setattr("chaos_agent.server.build", lambda: agent)
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["status", "--all"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["reverted_by"] == "request"

    monkeypatch.setenv("RAI_AGENT_STATE", str(tmp_path / "state.json"))
    assert main(["watchdog-health"]) == 1
    dog.heartbeat()
    assert main(["watchdog-health"]) == 0

    ran: list[Any] = []
    monkeypatch.setattr("chaos_agent.watchdog.Watchdog.run", lambda self, stop, i: ran.append(i))
    monkeypatch.setenv("RAI_WATCHDOG_INTERVAL", "2")
    assert main(["watchdog"]) == 0
    assert ran == [2.0]
