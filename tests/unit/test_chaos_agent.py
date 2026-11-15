"""The chaos agent against fake Toxiproxy and Docker backends (B-23: FR-07.1, NFR-SAF-1).

The real backends are exercised end to end by the CI job "Chaos agent"
(``scripts/chaos_e2e.py``).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import asdict
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from chaos_agent.agent import Agent
from chaos_agent.clients import BackendError
from chaos_agent.faults import FaultError, FaultRequest, StateFile, now, stamp
from chaos_agent.server import handler_for, main
from chaos_agent.signing import parse_keys
from rai.worker.chaos import Signer

LABEL = "rai.target=payments-api"
PROXY = "ledger-db-proxy"


class FakeToxiproxy:
    def __init__(self) -> None:
        self.proxies: dict[str, dict[str, Any]] = {
            "ledger-db-proxy": {"enabled": True, "toxics": {}}
        }
        self.calls: list[tuple[str, ...]] = []
        self.fail_next = False

    def proxy(self, name: str) -> dict[str, Any] | None:
        return self.proxies.get(name)

    def add_toxic(self, proxy: str, toxic: dict[str, Any]) -> None:
        if self.fail_next:
            self.fail_next = False
            raise BackendError("toxiproxy down")
        self.calls.append(("add_toxic", proxy, toxic["type"]))
        self.proxies[proxy]["toxics"][toxic["name"]] = toxic

    def remove_toxic(self, proxy: str, name: str) -> None:
        self.calls.append(("remove_toxic", proxy))
        self.proxies[proxy]["toxics"].pop(name, None)

    def set_enabled(self, proxy: str, enabled: bool) -> None:
        self.calls.append(("set_enabled", proxy, str(enabled)))
        self.proxies[proxy]["enabled"] = enabled


class FakeDocker:
    def __init__(self) -> None:
        self.containers = {
            "payments-api": {
                "Id": "c1",
                "Labels": {"rai.sandbox": "true", "rai.target": "payments-api"},
            },
            "rai-api": {"Id": "c2", "Labels": {"rai.target": "payments-api"}},  # not a sandbox
        }
        self.networks = {"c1": {"resilinet-ai_sandbox_net": {"Aliases": ["payments-api", "c1"]}}}
        self.calls: list[tuple[str, ...]] = []

    def find(self, service: str, project: str) -> list[dict[str, Any]]:
        if project != "sandboxbank":
            return []
        return [c for name, c in self.containers.items() if name == service]

    def project_of(self, container: str) -> str | None:
        return "sandboxbank"

    def inspect(self, container_id: str) -> dict[str, Any]:
        return {"NetworkSettings": {"Networks": self.networks.get(container_id, {})}}

    def action(self, container_id: str, verb: str) -> None:
        self.calls.append((verb, container_id))

    def disconnect(self, network: str, container_id: str) -> None:
        self.calls.append(("disconnect", network, container_id))

    def connect(self, network: str, container_id: str, aliases: list[str]) -> None:
        self.calls.append(("connect", network, container_id, ",".join(aliases)))


@pytest.fixture
def parts(tmp_path: Path) -> tuple[Agent, FakeToxiproxy, FakeDocker, StateFile]:
    state, toxiproxy, docker = StateFile(tmp_path / "state.json"), FakeToxiproxy(), FakeDocker()
    return Agent(state, toxiproxy, docker, "sandboxbank"), toxiproxy, docker, state


def request(kind: str, target: str = "payments-api", **extra: Any) -> FaultRequest:
    defaults: dict[str, Any] = {"duration_seconds": 60}
    if kind in ("latency", "timeout", "connection_drop"):
        target = "ledger-db-proxy"
    else:
        defaults["required_label"] = LABEL
    if kind == "latency":
        defaults["latency_ms"] = 200
    if kind == "network_partition":
        defaults["network"] = "sandbox_net"
    defaults["blast_radius"] = [target]
    return FaultRequest.parse({"type": kind, "target": target} | defaults | extra)


# ---- each fault type, applied and reverted ----------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "applied", "reverted"),
    [
        ("latency", ("add_toxic", PROXY, "latency"), ("remove_toxic", PROXY)),
        ("timeout", ("add_toxic", PROXY, "timeout"), ("remove_toxic", PROXY)),
        ("connection_drop", ("set_enabled", "ledger-db-proxy", "False"),
         ("set_enabled", "ledger-db-proxy", "True")),
        ("network_partition", ("disconnect", "resilinet-ai_sandbox_net", "c1"),
         ("connect", "resilinet-ai_sandbox_net", "c1", "payments-api,c1")),
        ("container_stop", ("stop", "c1"), ("start", "c1")),
        ("stale_upstream", ("pause", "c1"), ("unpause", "c1")),
    ],
)  # fmt: skip
def test_each_fault_is_applied_then_reverted(
    parts: Any, kind: str, applied: tuple[str, ...], reverted: tuple[str, ...]
) -> None:
    agent, toxiproxy, docker, state = parts
    fault = agent.inject(request(kind))
    calls = toxiproxy.calls + docker.calls
    assert calls == [applied]
    assert fault.status == "active"
    assert fault.onset is not None
    assert fault.expires_at is not None
    assert state.load()[fault.fault_id].status == "active"
    agent.revert(fault.fault_id)
    assert (toxiproxy.calls + docker.calls) == [applied, reverted]
    assert state.load()[fault.fault_id].status == "reverted"
    assert agent.open_faults() == []


def test_a_restart_is_instant(parts: Any) -> None:
    agent, _, docker, _ = parts
    fault = agent.inject(request("container_restart"))
    assert docker.calls == [("restart", "c1")]
    assert (fault.status, fault.reverted_at) == ("reverted", fault.onset)
    assert agent.open_faults() == []


# ---- what the agent refuses ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"type": "fork_bomb", "target": "x", "duration_seconds": 5}, "type must be one of"),
        ({"type": "latency", "target": "x", "duration_seconds": 5}, "latency needs latency_ms"),
        ({"type": "latency", "target": "x", "latency_ms": 5, "duration_seconds": 3601},
         "duration_seconds must be 1 to 3600"),
        ({"type": "latency", "target": "x", "latency_ms": 5, "duration_seconds": 901,
          "max_duration_seconds": 900}, "1 to 900"),
        ({"type": "container_stop", "target": "x", "duration_seconds": 5}, "need the target's"),
        ({"type": "network_partition", "target": "x", "duration_seconds": 5,
          "required_label": LABEL}, "needs the network"),
        ({"type": "latency", "target": "../etc", "latency_ms": 5, "duration_seconds": 5},
         "registered target name"),
        ({"type": "latency", "target": "x", "latency_ms": 5, "duration_seconds": 5, "cmd": "rm"},
         "unknown fields: cmd"),
        ([], "JSON object"),
        ({"type": "connection_drop", "target": "x", "duration_seconds": 5}, "blast_radius must"),
        ({"type": "connection_drop", "target": "x", "duration_seconds": 5,
          "blast_radius": ["y"]}, "outside the declared blast radius"),
        ({"type": "connection_drop", "target": "x", "duration_seconds": 5,
          "blast_radius": "x"}, "must be a list"),
        ({"type": "connection_drop", "target": "x", "duration_seconds": 5,
          "blast_radius": ["x"], "run_id": ""}, "run_id must be text"),
    ],
)  # fmt: skip
def test_requests_are_checked(body: Any, message: str) -> None:
    with pytest.raises(FaultError, match=message):
        FaultRequest.parse(body)


def test_targets_are_checked_against_the_sandbox(parts: Any) -> None:
    agent, _, docker, state = parts
    with pytest.raises(FaultError, match=r"not labelled rai\.sandbox=true"):
        agent.inject(request("container_stop", "rai-api"))
    with pytest.raises(FaultError, match=r"does not carry rai\.target=ledger-db"):
        agent.inject(request("container_stop", required_label="rai.target=ledger-db"))
    with pytest.raises(FaultError, match="must be exactly one container"):
        agent.inject(request("container_stop", "nobody"))
    with pytest.raises(FaultError, match="does not exist in Toxiproxy"):
        agent.inject(FaultRequest.parse({"type": "connection_drop", "target": "nope",
                                         "duration_seconds": 5,
                                         "blast_radius": ["nope"]}))  # fmt: skip
    with pytest.raises(FaultError, match="not on network 'other_net'"):
        agent.inject(request("network_partition", network="other_net"))
    assert docker.calls == []
    assert state.load() == {}  # refused before anything was recorded or done


def test_container_faults_stay_inside_the_agents_project(parts: Any) -> None:
    _, toxiproxy, docker, state = parts
    elsewhere = Agent(state, toxiproxy, docker, "someone-elses-stack")
    with pytest.raises(FaultError, match="must be exactly one container"):
        elsewhere.inject(request("container_stop"))
    unknown = Agent(state, toxiproxy, docker, None)
    with pytest.raises(FaultError, match="could not tell its own Compose project"):
        unknown.inject(request("container_stop"))
    assert unknown.inject(request("connection_drop")).status == "active"  # proxies still work


def test_the_expiry_loop_never_undoes_an_injection_in_progress(parts: Any) -> None:
    """Regression: the loop saw a fault still 'injecting' (no expiry yet), waited for the
    lock, and reverted it the moment the injection finished (found by the CI e2e)."""
    agent, _, docker, state = parts
    original = docker.action

    def slow_action(container_id: str, verb: str) -> None:
        assert agent.revert_expired() == []  # runs while the fault is still 'injecting'
        original(container_id, verb)

    docker.action = slow_action
    fault = agent.inject(request("container_stop"))
    assert state.load()[fault.fault_id].status == "active"
    assert docker.calls == [("stop", "c1")]


def test_one_fault_per_target(parts: Any) -> None:
    agent, *_ = parts
    agent.inject(request("container_stop"))
    with pytest.raises(FaultError, match="already has an active fault"):
        agent.inject(request("stale_upstream"))


def test_the_fault_is_recorded_before_it_acts_and_undone_if_it_fails(parts: Any) -> None:
    agent, toxiproxy, _, state = parts
    seen: list[str] = []
    original = toxiproxy.add_toxic

    def spy(proxy: str, toxic: dict[str, Any]) -> None:
        seen.extend(f.status for f in state.load().values())
        original(proxy, toxic)

    toxiproxy.add_toxic = spy
    agent.inject(request("latency"))
    assert seen == ["injecting"]  # persisted before Toxiproxy was touched (R-03)

    agent.revert_all()
    toxiproxy.fail_next = True
    with pytest.raises(BackendError):
        agent.inject(request("timeout"))
    assert [f.status for f in state.load().values()].count("failed") == 1
    assert agent.open_faults() == []


# ---- expiry, crash recovery, kill --all ------------------------------------------------------


def test_expired_faults_are_reverted(parts: Any) -> None:
    agent, _, docker, state = parts
    fault = agent.inject(request("container_stop"))
    assert agent.revert_expired() == []
    fault.expires_at = stamp(now() - timedelta(seconds=1))
    state.put(fault)
    assert [f.fault_id for f in agent.revert_expired()] == [fault.fault_id]
    assert docker.calls[-1] == ("start", "c1")


def test_faults_left_by_a_crash_are_reverted_from_the_state_file(
    parts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent, _, docker, state = parts
    agent.inject(request("stale_upstream"))
    agent.inject(request("connection_drop"))
    restarted = Agent(StateFile(state.path), agent.toxiproxy, docker, "sandboxbank")
    assert {f.request.type for f in restarted.open_faults()} == {
        "stale_upstream",
        "connection_drop",
    }

    monkeypatch.setattr("chaos_agent.server.build", lambda: restarted)
    assert main(["kill", "--all"]) == 0
    assert "reverted 2 fault(s)" in capsys.readouterr().out
    assert ("unpause", "c1") in docker.calls
    assert restarted.open_faults() == []
    assert json.loads((tmp_path / "state.json").read_text())["format"] == "rai-chaos-state/1"


# ---- the HTTP interface ----------------------------------------------------------------------


ENV = "SANDBOXBANK"


@pytest.fixture
def signer() -> Signer:
    return Signer.generate()


@pytest.fixture
def served(parts: Any, signer: Signer) -> Iterator[tuple[str, Agent]]:
    """The agent's HTTP interface, trusting ``signer``'s public key, serving ENV."""
    agent = parts[0]
    agent.environment = ENV
    handler = handler_for(agent, parse_keys(signer.public_key))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", agent
    server.shutdown()


def call(url: str, method: str = "GET", body: Any = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as response:  # noqa: S310 - local test server
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def signed_body(
    kind: str, step: str = "item-1", run: str = "run-1", **extra: Any
) -> dict[str, Any]:
    """A request as the worker signs it: with its run, step, environment and tenant."""
    body = asdict(request(kind, **extra))
    return body | {"run_id": run, "step_key": step, "environment": ENV, "tenant_id": "t-1",
                   "blast_radius": list(body["blast_radius"])}  # fmt: skip


def test_only_requests_signed_by_a_trusted_worker_are_applied(served: Any, signer: Signer) -> None:
    url, agent = served
    plain = {"type": "container_stop", "target": "payments-api", "duration_seconds": 30,
             "required_label": LABEL, "blast_radius": ["payments-api"]}  # fmt: skip
    status, answer = call(f"{url}/faults", "POST", plain)
    assert status == 403
    assert "send a signed request" in answer["error"]
    stranger = Signer.generate().sign(signed_body("container_stop"))
    status, answer = call(f"{url}/faults", "POST", stranger)
    assert status == 403
    assert answer["error"] == "the request is signed with a key the agent does not trust"
    assert agent.open_faults() == []

    status, fault = call(f"{url}/faults", "POST", signer.sign(signed_body("container_stop")))
    assert status == 201
    assert fault["onset"] is not None
    assert fault["signed_by"] == signer.key_id
    assert fault["request"]["run_id"] == "run-1"
    assert call(f"{url}/faults")[1]["faults"][0]["fault_id"] == fault["fault_id"]
    assert call(f"{url}/faults/{fault['fault_id']}", "DELETE")[1]["status"] == "reverted"
    assert call(f"{url}/faults/nope", "DELETE")[0] == 404

    call(f"{url}/faults", "POST", signer.sign(signed_body("container_stop", step="item-2")))
    assert len(call(f"{url}/kill", "POST", {})[1]["reverted"]) == 1  # kill is never signed
    assert call(f"{url}/healthz") == (200, {"status": "ok"})
    assert call(f"{url}/nope")[0] == 404


def test_a_signed_request_is_used_once(served: Any, signer: Signer) -> None:
    """Threat S-03: a replayed request, or a second fault for the same run and step,
    is refused with the fault already recorded (so a retrying worker learns the onset)."""
    url, agent = served
    envelope = signer.sign(signed_body("connection_drop"))
    first = call(f"{url}/faults", "POST", envelope)
    assert first[0] == 201
    status, answer = call(f"{url}/faults", "POST", envelope)
    assert status == 409
    assert answer["fault"]["fault_id"] == first[1]["fault_id"]
    agent.revert_all()  # even once reverted, the run and step stay used
    again = signer.sign(signed_body("connection_drop"))
    assert call(f"{url}/faults", "POST", again)[0] == 409
    other_run = signer.sign(signed_body("connection_drop", run="run-2"))
    assert call(f"{url}/faults", "POST", other_run)[0] == 201


def test_the_http_interface_reports_problems(served: Any, signer: Signer) -> None:
    url, agent = served
    toxiproxy = agent.toxiproxy
    bad = signed_body("latency") | {"type": "nope"}
    assert call(f"{url}/faults", "POST", signer.sign(bad))[0] == 422
    assert call(f"{url}/elsewhere", "POST", {})[0] == 404
    assert call(f"{url}/elsewhere", "DELETE")[0] == 404
    for raw in (b"", b"not json"):
        empty = urllib.request.Request(f"{url}/faults", data=raw, method="POST")  # noqa: S310
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(empty, timeout=5)  # noqa: S310 - local test server
        assert refused.value.code == 400

    toxiproxy.fail_next = True
    status, answer = call(f"{url}/faults", "POST", signer.sign(signed_body("latency")))
    assert status == 502
    assert "undone" in answer["error"]

    fault = agent.inject(request("connection_drop", environment=ENV))
    original = toxiproxy.set_enabled

    def broken(proxy: str, enabled: bool) -> None:
        raise BackendError("toxiproxy down")

    toxiproxy.set_enabled = broken
    assert call(f"{url}/faults/{fault.fault_id}", "DELETE")[0] == 502
    toxiproxy.set_enabled = original


# ---- the real HTTP clients, against a recording server ---------------------------------------


@pytest.fixture
def backend() -> Iterator[tuple[str, list[tuple[str, str, Any]], dict[str, tuple[int, Any]]]]:
    """A local HTTP server that records requests and answers from a table."""
    from http.server import BaseHTTPRequestHandler

    seen: list[tuple[str, str, Any]] = []
    answers: dict[str, tuple[int, Any]] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            seen.append((self.command, self.path, body))
            status, payload = answers.get(f"{self.command} {self.path.split('?')[0]}", (200, {}))
            raw = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_DELETE = _answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen, answers
    server.shutdown()


def test_the_toxiproxy_client(backend: Any) -> None:
    from chaos_agent.clients import HttpToxiproxy

    url, seen, answers = backend
    proxy = HttpToxiproxy(url)
    answers["GET /proxies/gone"] = (404, {"error": "proxy not found"})
    answers["DELETE /proxies/p/toxics/old"] = (404, {"error": "toxic not found"})
    answers["POST /proxies/broken"] = (500, {"error": "boom"})
    assert proxy.proxy("gone") is None
    assert proxy.proxy("p") == {}
    proxy.add_toxic("p", {"name": "t", "type": "latency"})
    proxy.remove_toxic("p", "old")  # already gone: fine
    proxy.set_enabled("p", False)
    with pytest.raises(BackendError, match="HTTP 500"):
        proxy.set_enabled("broken", True)
    assert [(m, p) for m, p, _ in seen] == [
        ("GET", "/proxies/gone"), ("GET", "/proxies/p"), ("POST", "/proxies/p/toxics"),
        ("DELETE", "/proxies/p/toxics/old"), ("POST", "/proxies/p"), ("POST", "/proxies/broken"),
    ]  # fmt: skip
    assert seen[4][2] == {"enabled": False}


def test_the_docker_client_sends_only_the_allowed_calls(backend: Any) -> None:
    from chaos_agent.clients import HttpDocker

    url, seen, answers = backend
    docker = HttpDocker(url)
    answers["GET /v1.41/containers/me/json"] = (200, {"Config": {"Labels": {
        "com.docker.compose.project": "sandboxbank"}}})  # fmt: skip
    answers["GET /v1.41/containers/nobody/json"] = (404, {"message": "no such container"})
    answers["POST /v1.41/containers/c1/stop"] = (304, None)  # already stopped
    answers["POST /v1.41/containers/c1/start"] = (500, {"message": "boom"})
    answers["GET /v1.41/containers/json"] = (200, [{"Id": "c1"}])
    assert docker.find("payments-api", "sandboxbank") == [{"Id": "c1"}]
    assert "com.docker.compose.project%3Dsandboxbank" in seen[0][1]
    assert docker.project_of("me") == "sandboxbank"
    assert docker.project_of("nobody") is None
    docker.action("c1", "stop")  # 304 is success
    with pytest.raises(BackendError, match="HTTP 500"):
        docker.action("c1", "start")
    with pytest.raises(BackendError, match="not an allowed container action"):
        docker.action("c1", "exec")
    docker.disconnect("net", "c1")
    docker.connect("net", "c1", ["payments-api"])
    assert seen[-2:] == [
        ("POST", "/v1.41/networks/net/disconnect", {"Container": "c1", "Force": True}),
        ("POST", "/v1.41/networks/net/connect",
         {"Container": "c1", "EndpointConfig": {"Aliases": ["payments-api"]}}),
    ]  # fmt: skip
    assert not any("exec" in path for _, path, _ in seen)


def test_an_unreachable_backend_is_a_backend_error() -> None:
    from chaos_agent.clients import HttpToxiproxy

    with pytest.raises(BackendError):
        HttpToxiproxy("http://127.0.0.1:9").set_enabled("p", True)


def test_build_reads_its_configuration(tmp_path: Path) -> None:
    from chaos_agent.server import build

    agent = build({"RAI_AGENT_STATE": str(tmp_path / "s.json"), "RAI_AGENT_COMPOSE_PROJECT": "x",
                   "RAI_AGENT_DOCKER_URL": "http://127.0.0.1:9"})  # fmt: skip
    assert agent.project == "x"
    assert agent.state.path == tmp_path / "s.json"
    assert build({"RAI_AGENT_STATE": str(tmp_path / "t.json"),
                  "RAI_AGENT_DOCKER_URL": "http://127.0.0.1:9"}).project is None  # fmt: skip


def test_status_lists_open_faults(
    parts: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = parts[0]
    agent.inject(request("container_stop"))
    monkeypatch.setattr("chaos_agent.server.build", lambda: agent)
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["request"]["type"] == "container_stop"
