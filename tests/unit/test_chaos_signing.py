"""Signed fault requests (B-24: threats S-03, D-02): the worker's signer and the agent's
verifier together, the agent's own limits, and the worker client's parts that need
no database. The database half (worker preflight, observations) is in
tests/integration/test_chaos_worker.py; the real agent in CI (scripts/chaos_e2e.py).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from typer.testing import CliRunner

from chaos_agent.agent import Agent, FaultReplay
from chaos_agent.faults import FaultError, FaultRequest, StateFile
from chaos_agent.server import build, trusted_keys
from chaos_agent.signing import SignatureError, parse_keys, verify
from rai.cli.main import app
from rai.worker.chaos import (
    AgentClient,
    ChaosSetupError,
    FaultSpec,
    PreparedFault,
    Signer,
    _answer,
    from_env,
)
from tests.unit.test_chaos_agent import FakeDocker, FakeToxiproxy

ENV = "SANDBOXBANK"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def body(**extra: Any) -> dict[str, Any]:
    return {"type": "connection_drop", "target": "ledger-db-proxy", "duration_seconds": 60,
            "run_id": "r1", "step_key": "item-1", "environment": ENV,
            "blast_radius": ["ledger-db-proxy"]} | extra  # fmt: skip


@pytest.fixture
def signer() -> Signer:
    return Signer.generate()


def check(envelope: Any, signer: Signer, now: datetime = NOW, env: str | None = ENV) -> Any:
    return verify(envelope, parse_keys(signer.public_key), environment=env, now=now)


def test_a_request_signed_by_the_worker_verifies_on_the_agent(signer: Signer) -> None:
    verified = check(signer.sign(body(), now=NOW), signer)
    assert verified.key_id == signer.key_id
    assert verified.request == body()
    assert FaultRequest.parse(verified.request).run_id == "r1"


def _tampered(envelope: dict[str, str]) -> dict[str, str]:
    raw = base64.urlsafe_b64decode(envelope["payload"] + "==")
    changed = raw.replace(b'"duration_seconds":60', b'"duration_seconds":900')
    assert changed != raw
    return envelope | {"payload": base64.urlsafe_b64encode(changed).decode().rstrip("=")}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda e, s: _tampered(e), "the signature does not match"),
        (lambda e, s: e | {"signature": e["signature"][::-1]}, "signature"),
        (lambda e, s: e | {"payload": "%%%"}, "not valid base64url"),
        (lambda e, s: e | {"key_id": "0" * 16}, "a key the agent does not trust"),
        (lambda e, s: e | {"extra": 1}, "exactly format, key_id, payload and signature"),
        (lambda e, s: e | {"format": "rai-signed-fault/0"}, "send a signed request"),
        (lambda e, s: s.sign(body(), now=NOW + timedelta(seconds=31)), "issued in the future"),
        (lambda e, s: s.sign(body(), now=NOW - timedelta(seconds=61)), "has expired"),
        (lambda e, s: s.sign(body(environment="STAGING-01"), now=NOW), "this agent serves"),
        (lambda e, s: s.sign(body(run_id=None), now=NOW), "names its run_id"),
        (lambda e, s: s.sign(body(step_key=""), now=NOW), "names its step_key"),
    ],
)  # fmt: skip
def test_the_agent_refuses_what_it_cannot_attribute(
    signer: Signer, change: Any, message: str
) -> None:
    with pytest.raises(SignatureError, match=message):
        check(change(signer.sign(body(), now=NOW), signer), signer)


def _signed_payload(signer: Signer, payload: dict[str, Any]) -> dict[str, str]:
    """An envelope around an arbitrary payload, signed exactly as the worker signs."""
    from rai.worker.chaos import DOMAIN, _b64

    raw = json.dumps(payload).encode()
    return {"format": "rai-signed-fault/1", "key_id": signer.key_id, "payload": _b64(raw),
            "signature": _b64(signer._key.sign(DOMAIN + raw))}  # fmt: skip


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"format": "x"}, "must be rai-fault-request/1"),
        ({"format": "rai-fault-request/1", "issued_at": "2026-11-15T12:00:00Z",
          "expires_at": "2026-11-15T12:05:00Z", "request": body()}, "at most 120 s"),
        ({"format": "rai-fault-request/1", "issued_at": "2026-11-15T12:00:00",
          "expires_at": "2026-11-15T12:01:00Z", "request": body()}, "time zone"),
        ({"format": "rai-fault-request/1", "issued_at": "noon",
          "expires_at": "2026-11-15T12:01:00Z", "request": body()}, "not a timestamp"),
        ({"format": "rai-fault-request/1", "issued_at": "2026-11-15T12:00:00Z",
          "expires_at": "2026-11-15T12:01:00Z", "request": []}, "has no request"),
    ],
)  # fmt: skip
def test_the_payload_is_checked_after_the_signature(
    signer: Signer, payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(SignatureError, match=message):
        check(_signed_payload(signer, payload), signer)
    raw = {"format": "rai-signed-fault/1", "key_id": signer.key_id, "payload": "bm90IGpzb24",
           "signature": ""}  # fmt: skip
    raw["signature"] = _signed_payload(signer, {})["signature"]
    with pytest.raises(SignatureError):
        check(raw, signer)


def test_an_agent_without_keys_or_environment_refuses_everything(signer: Signer) -> None:
    envelope = signer.sign(body(), now=NOW)
    with pytest.raises(SignatureError, match="trusts no worker key"):
        verify(envelope, {}, environment=ENV, now=NOW)
    with pytest.raises(SignatureError, match="has no environment"):
        check(envelope, signer, env=None)


def test_worker_keys_are_parsed_strictly(signer: Signer) -> None:
    other = Signer.generate()
    keys = parse_keys(f" {signer.public_key}, ,{other.public_key}")
    assert set(keys) == {signer.key_id, other.key_id}
    with pytest.raises(SignatureError, match="32-byte"):
        parse_keys(base64.b64encode(b"short").decode())
    with pytest.raises(SignatureError, match="base64"):
        parse_keys("not*base64")
    assert trusted_keys({}) == {}


# ---- the agent's own limits (second preflight) ------------------------------------------------


@pytest.fixture
def agent(tmp_path: Path) -> Agent:
    return Agent(StateFile(tmp_path / "state.json"), FakeToxiproxy(), FakeDocker(), "sandboxbank",
                 environment=ENV, max_fault_seconds=300, max_open_faults=2)  # fmt: skip


def proxy(target: str = "ledger-db-proxy", **extra: Any) -> FaultRequest:
    return FaultRequest.parse(body(target=target, blast_radius=[target]) | extra)


def test_the_agent_applies_its_own_limits_whatever_the_request_says(agent: Agent) -> None:
    """Threat D-02: the agent's ceiling and open-fault cap hold even for a request the
    worker signed with a larger environment ceiling."""
    agent.toxiproxy.proxies |= {"analytics-db-proxy": {"enabled": True, "toxics": {}},
                                "third-proxy": {"enabled": True, "toxics": {}}}  # fmt: skip
    with pytest.raises(FaultError, match="this agent serves environment 'SANDBOXBANK' only"):
        agent.inject(proxy(environment="STAGING-01"))
    with pytest.raises(FaultError, match="exceeds this agent's ceiling of 300 s"):
        agent.inject(proxy(duration_seconds=301, max_duration_seconds=900))
    agent.inject(proxy(run_id="a"))
    agent.inject(proxy("analytics-db-proxy", run_id="b"))
    with pytest.raises(FaultError, match="2 faults are open; this agent allows 2 at once"):
        agent.inject(proxy("third-proxy", run_id="c"))
    assert agent.state.load().__len__() == 2


def test_a_run_keeps_the_blast_radius_it_declared(agent: Agent) -> None:
    agent.toxiproxy.proxies["analytics-db-proxy"] = {"enabled": True, "toxics": {}}
    radius = ["analytics-db-proxy", "ledger-db-proxy"]
    agent.inject(proxy(step_key="a", blast_radius=radius))
    with pytest.raises(FaultError, match="keeps the blast radius it declared"):
        agent.inject(proxy("analytics-db-proxy", step_key="b", blast_radius=["analytics-db-proxy"]))
    assert agent.inject(proxy("analytics-db-proxy", step_key="b", blast_radius=radius[::-1]))


SCENARIO = "0190aaaa-0000-7000-8000-000000000001@1"
WIDE = ["analytics-db-proxy", "ledger-db-proxy"]


def step(n: int, item: int = 1) -> str:
    return f"item-{item}.fault-{n}"


def scenario_fault(target: str, n: int, radius: list[str], **extra: Any) -> FaultRequest:
    return proxy(target, step_key=step(n), blast_radius=radius, scenario=SCENARIO,
                 **extra)  # fmt: skip


def test_a_scenario_keeps_its_radius_after_its_first_fault_is_reverted(agent: Agent) -> None:
    """B-27 (D-02): the radius is the scenario's, not just the open faults'. Without
    this, fault 2 could widen the reach once fault 1 had been reverted."""
    agent.toxiproxy.proxies["analytics-db-proxy"] = {"enabled": True, "toxics": {}}
    first = agent.inject(scenario_fault("ledger-db-proxy", 1, ["ledger-db-proxy"]))
    agent.revert(first.fault_id)
    with pytest.raises(FaultError, match="keeps the blast radius it declared with its first"):
        agent.inject(scenario_fault("analytics-db-proxy", 2, WIDE))
    with pytest.raises(FaultError, match="already ran scenario"):
        agent.inject(proxy(step_key=step(2), blast_radius=["ledger-db-proxy"],
                           scenario=SCENARIO.replace("1@1", "2@1")))  # fmt: skip
    assert agent.inject(scenario_fault("ledger-db-proxy", 2, ["ledger-db-proxy"]))


def test_each_scenario_step_of_a_run_has_its_own_radius(agent: Agent) -> None:
    agent.toxiproxy.proxies["analytics-db-proxy"] = {"enabled": True, "toxics": {}}
    first = agent.inject(scenario_fault("ledger-db-proxy", 1, ["ledger-db-proxy"]))
    agent.revert(first.fault_id)
    other = proxy("analytics-db-proxy", step_key=step(1, item=2), blast_radius=WIDE,
                  scenario=SCENARIO.replace("1@1", "2@1"))  # fmt: skip
    assert agent.inject(other).request.scenario_step == ("r1", "item-2")


@pytest.mark.parametrize(
    "extra",
    [{"step_key": "item-1"}, {"step_key": None}, {"run_id": None}, {"step_key": "a.fault-0"}],
)
def test_a_scenario_fault_names_its_run_and_step(extra: dict[str, Any]) -> None:
    with pytest.raises(FaultError, match=r"needs run_id and a step_key like item-1.fault-2"):
        proxy(scenario=SCENARIO, **({"step_key": step(1)} | extra))


def test_the_replay_guard_survives_a_restart(agent: Agent) -> None:
    first = agent.inject(proxy())
    restarted = Agent(StateFile(agent.state.path), agent.toxiproxy, agent.docker, "sandboxbank",
                      environment=ENV)  # fmt: skip
    with pytest.raises(FaultReplay) as replay:
        restarted.inject(proxy())
    assert replay.value.fault.fault_id == first.fault_id


def test_build_reads_the_signing_configuration(tmp_path: Path) -> None:
    built = build({"RAI_AGENT_STATE": str(tmp_path / "s.json"), "RAI_AGENT_COMPOSE_PROJECT": "x",
                   "RAI_AGENT_ENVIRONMENT": ENV, "RAI_AGENT_MAX_FAULT_SECONDS": "7200",
                   "RAI_AGENT_MAX_OPEN_FAULTS": "1"})  # fmt: skip
    assert (built.environment, built.max_fault_seconds, built.max_open_faults) == (ENV, 3600, 1)


# ---- the worker client ------------------------------------------------------------------------


def spec(**extra: Any) -> FaultSpec:
    return FaultSpec.model_validate(
        {
            "type": "latency",
            "target": "ledger-db-proxy",
            "duration_seconds": 60,
            "latency_ms": 200,
            "blast_radius": ["ledger-db-proxy"],
        }
        | extra
    )


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"blast_radius": ["other"]}, "outside the declared blast radius"),
        ({"latency_ms": None}, "latency needs latency_ms"),
        ({"type": "network_partition"}, "needs the network"),
        ({"blast_radius": []}, "at least 1"),
        ({"duration_seconds": 3601}, "less than or equal to 3600"),
        ({"command": "rm"}, "Extra inputs"),
    ],
)  # fmt: skip
def test_fault_specs_are_checked(extra: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        spec(**extra)


def prepared() -> PreparedFault:
    return PreparedFault(spec(), uuid4(), uuid4(), "item-1", ENV, 900, "rai.target=toxiproxy")


def test_the_signed_request_names_the_scenario_version() -> None:
    from rai.core.scenarios import ScenarioDefinition, scenario_ref

    sid = uuid4()
    definition = ScenarioDefinition.model_validate({
        "blast_radius": ["ledger-db-proxy"],
        "faults": [{"type": "connection_drop", "target": "ledger-db-proxy",
                    "duration_seconds": 5}],
    })  # fmt: skip
    (only,) = definition.specs(scenario_ref(sid, 3))
    fault = PreparedFault(only, uuid4(), uuid4(), "item-1.fault-1", ENV, 900, "rai.target=x")
    request = FaultRequest.parse(fault.request())
    assert (request.scenario, request.scenario_step) == (f"{sid}@3", (str(fault.run_id), "item-1"))
    assert prepared().request()["scenario"] is None  # a fault outside a scenario


def test_the_agents_answers_become_outcomes() -> None:
    fault = prepared()
    mine = {"run_id": str(fault.run_id), "step_key": "item-1"}
    onset = {"fault_id": "f1", "onset": "2026-11-15T12:00:00Z", "request": mine}
    assert _answer(fault, 201, onset).status == "injected"
    assert _answer(fault, 201, onset).onset == "2026-11-15T12:00:00Z"
    retried = _answer(fault, 409, {"error": "used", "fault": onset})
    assert (retried.status, retried.fault) == ("injected", onset)  # a retry learns its onset
    failed = _answer(fault, 409, {"error": "used", "fault": {"request": mine, "error": "boom"}})
    assert (failed.status, failed.reasons) == ("error", ("boom",))
    other = {"fault_id": "f2", "onset": "x", "request": {"run_id": "else", "step_key": "item-1"}}
    assert _answer(fault, 409, {"error": "used", "fault": other}).status == "refused"
    assert _answer(fault, 403, {"error": "bad signature"}).reasons == ("bad signature",)
    assert _answer(fault, 422, {"error": "no"}).status == "refused"
    assert _answer(fault, 0, {"error": "unreachable"}).status == "error"
    assert _answer(fault, 502, "oops").reasons == ("HTTP 502",)


def test_the_signed_request_carries_the_registry_label_and_ceiling() -> None:
    fault = prepared()
    request = fault.request()
    assert request["required_label"] == "rai.target=toxiproxy"
    assert request["max_duration_seconds"] == 900
    assert request["environment"] == ENV
    assert FaultRequest.parse(request).run_id == str(fault.run_id)  # the agent accepts the shape


def test_an_unreachable_agent_is_an_answer_not_an_exception(signer: Signer) -> None:
    status, answer = AgentClient("http://127.0.0.1:9", timeout=2).post_fault(signer.sign(body()))
    assert status == 0
    assert "unreachable" in answer["error"]
    with pytest.raises(ChaosSetupError, match="http"):
        AgentClient("file:///etc/passwd")


def test_the_worker_reads_its_chaos_configuration(tmp_path: Path, signer: Signer) -> None:
    key = tmp_path / "worker.pem"
    key.write_bytes(signer.pem())
    assert from_env({}) is None
    client = from_env({"RAI_WORKER_SIGNING_KEY": f"file:{key.as_posix()}",
                       "RAI_WORKER_CHAOS_AGENT_URL": "http://agent:8090"})  # fmt: skip
    assert client is not None
    assert client.signer.key_id == signer.key_id
    assert "PRIVATE" not in repr(client.signer)
    with pytest.raises(ChaosSetupError, match="both"):
        from_env({"RAI_WORKER_CHAOS_AGENT_URL": "http://agent:8090"})
    with pytest.raises(ChaosSetupError, match="does not exist"):
        from_env({"RAI_WORKER_SIGNING_KEY": "file:/nowhere/key.pem",
                  "RAI_WORKER_CHAOS_AGENT_URL": "http://agent:8090"})  # fmt: skip
    ec = generate_private_key(SECP256R1()).private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    with pytest.raises(ChaosSetupError, match="Ed25519"):
        Signer.from_pem(ec)


def test_keygen_writes_the_private_key_and_prints_only_the_public_key(tmp_path: Path) -> None:
    out = tmp_path / "keys" / "worker.pem"
    result = CliRunner().invoke(app, ["admin", "chaos-keygen", "--out", str(out)])
    assert result.exit_code == 0, result.output
    public = result.stdout.strip()
    assert parse_keys(public)  # exactly what RAI_AGENT_WORKER_KEYS takes
    assert Signer.from_pem(out.read_bytes()).public_key == public
    again = CliRunner().invoke(app, ["admin", "chaos-keygen", "--out", str(out)])
    assert again.exit_code == 2
    assert "never overwritten" in again.output
