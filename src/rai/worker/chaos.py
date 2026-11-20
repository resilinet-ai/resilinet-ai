"""The worker's half of fault injection (B-24: threats S-03, S-05, R-03, D-02; FR-07.1).

The worker never injects a fault itself; it asks the chaos agent, in four steps:

1. **Worker preflight** (:func:`fault_preflight`). The run must have an active
   environment; every target in the declared blast radius must be on that
   environment's allow-list (B-22) with the right kind (``proxy`` for latency,
   timeout and connection drop, ``container`` for the rest); the duration must be
   within the environment's ceiling. The label the target must carry comes from the
   registry, never from the test definition.
2. **Signing** (:class:`Signer`). The request is signed with the worker's Ed25519
   key (``RAI_WORKER_SIGNING_KEY``, a secret reference to a PKCS#8 PEM file made by
   ``rai admin chaos-keygen``), names the run, the step and the environment, and
   is valid for 60 s. The agent verifies it (``chaos_agent.signing``), refuses a
   second fault for the same run and step, and runs its own checks again.
3. **Sending** (:class:`AgentClient`) to ``RAI_WORKER_CHAOS_AGENT_URL``.
4. **Recording** (:func:`record`). The onset (t0) of an injected fault, or why it
   was refused, becomes an observation of the run. Recording is idempotent: when a
   worker retries a step after a crash, the agent answers 409 with the fault it
   already injected and the worker records that onset, once.

Scenario steps (B-26) call :meth:`ChaosClient.inject` inside their step transaction.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)
from sqlalchemy import Connection, text

from rai.connectors.secrets import SecretRef
from rai.core.runs import Pins
from rai.core.scenarios import PROXY_FAULTS, FaultSpec, latest_decision

SIGNING_KEY_ENV = "RAI_WORKER_SIGNING_KEY"
AGENT_URL_ENV = "RAI_WORKER_CHAOS_AGENT_URL"
ENVELOPE_FORMAT = "rai-signed-fault/1"  # the agent's chaos_agent.signing, byte for byte
PAYLOAD_FORMAT = "rai-fault-request/1"
DOMAIN = (ENVELOPE_FORMAT + "\n").encode("ascii")
LIFETIME = timedelta(seconds=60)  # the agent accepts at most 120 s


class ChaosSetupError(RuntimeError):
    """The worker cannot request faults with this configuration."""


@dataclass(frozen=True)
class PreparedFault:
    """A fault the worker preflight accepted: what will be signed."""

    spec: FaultSpec
    run_id: UUID
    tenant_id: UUID
    step_key: str
    environment: str
    ceiling_seconds: int
    required_label: str
    # B-43: when the scenario's approval lapses; the agent refuses the fault after it.
    approval_expires_at: datetime | None = None

    def request(self) -> dict[str, Any]:
        spec = self.spec
        return {
            "type": spec.type, "target": spec.target, "duration_seconds": spec.duration_seconds,
            "required_label": self.required_label, "latency_ms": spec.latency_ms,
            "jitter_ms": spec.jitter_ms, "network": spec.network, "run_id": str(self.run_id),
            "step_key": self.step_key, "tenant_id": str(self.tenant_id),
            "environment": self.environment, "max_duration_seconds": self.ceiling_seconds,
            "blast_radius": sorted(spec.blast_radius), "scenario": spec.scenario,
            "approval_expires_at": (
                _stamp(self.approval_expires_at) if self.approval_expires_at else None
            ),
        }  # fmt: skip


def fault_preflight(
    conn: Connection, pins: Pins, spec: FaultSpec, *, run_id: UUID, tenant_id: UUID, step_key: str
) -> tuple[PreparedFault | None, list[str]]:
    """The worker's checks against the registry (deny by default, NFR-SAF-1).

    ``conn`` carries the run's tenant context, so only that tenant's environment and
    allow-list are visible (RLS)."""
    if pins.environment is None:
        return None, ["the run has no environment; faults need a registered environment"]
    env = conn.execute(
        text(
            "SELECT name, status::text AS status, "
            "CAST(extract(epoch FROM max_fault_duration) AS integer) AS ceiling "
            "FROM rai.environments WHERE environment_id = :e"
        ),
        {"e": pins.environment.environment_id},
    ).first()
    if env is None:
        return None, [f"environment {pins.environment.name!r} is not registered"]
    problems: list[str] = []
    approval_expires_at: datetime | None = None
    if spec.scenario is not None:  # B-43: the approval must still be in force, fault by fault
        decision = latest_decision(conn, UUID(spec.scenario.partition("@")[0]))
        refusal = "the scenario is not approved" if decision is None else decision.refusal()
        if refusal is not None:
            problems.append(refusal)
        elif decision is not None:
            approval_expires_at = decision.expires_at
    if env.status != "active":
        problems.append(f"environment {env.name!r} is retired")
    if spec.duration_seconds > env.ceiling:
        problems.append(f"duration {spec.duration_seconds} s exceeds environment {env.name!r}'s "
                        f"ceiling of {env.ceiling} s")  # fmt: skip
    targets = {
        row.name: row
        for row in conn.execute(
            text(
                "SELECT name, kind::text AS kind, required_label FROM rai.environment_targets "
                "WHERE environment_id = :e AND name = ANY(:names)"
            ),
            {"e": pins.environment.environment_id, "names": list(spec.blast_radius)},
        )
    }
    missing = sorted(set(spec.blast_radius) - set(targets))
    if missing:
        problems.append(f"not on environment {env.name!r}'s allow-list: {', '.join(missing)}")
    target = targets.get(spec.target)
    wanted = "proxy" if spec.type in PROXY_FAULTS else "container"
    if target is not None and target.kind != wanted:
        problems.append(f"{spec.type} needs a {wanted} target; {spec.target!r} is a {target.kind}")
    if target is not None and not target.required_label:
        problems.append(f"target {spec.target!r} has no required label")
    if problems or target is None:
        return None, problems
    return (
        PreparedFault(spec, run_id, tenant_id, step_key, env.name, env.ceiling,
                      target.required_label, approval_expires_at),
        [],
    )  # fmt: skip


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Signer:
    """The worker's Ed25519 key. The private key never leaves this object."""

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_key = base64.b64encode(public).decode("ascii")  # for RAI_AGENT_WORKER_KEYS
        self.key_id = hashlib.sha256(public).hexdigest()[:16]

    def __repr__(self) -> str:
        return f"Signer(key_id={self.key_id})"

    @classmethod
    def generate(cls) -> Signer:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_pem(cls, pem: bytes) -> Signer:
        key = load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ChaosSetupError("the signing key must be an Ed25519 private key")
        return cls(key)

    @classmethod
    def from_ref(cls, ref: str) -> Signer:
        """A secret reference (``file:/run/secrets/rai_worker_signing_key`` or ``env:NAME``)."""
        return cls.from_pem(SecretRef.parse(ref).resolve().reveal().encode("ascii"))

    def pem(self) -> bytes:
        return self._key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())

    def sign(self, request: dict[str, Any], *, now: datetime | None = None) -> dict[str, str]:
        issued = now or datetime.now(UTC)
        payload = json.dumps(
            {"format": PAYLOAD_FORMAT, "issued_at": _stamp(issued),
             "expires_at": _stamp(issued + LIFETIME), "request": request},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")  # fmt: skip
        return {
            "format": ENVELOPE_FORMAT,
            "key_id": self.key_id,
            "payload": _b64(payload),
            "signature": _b64(self._key.sign(DOMAIN + payload)),
        }


class AgentClient:
    def __init__(self, url: str, timeout: float = 30) -> None:
        if not url.startswith(("http://", "https://")):
            raise ChaosSetupError(f"{AGENT_URL_ENV} must be an http(s) URL")
        self.url, self.timeout = url.rstrip("/"), timeout

    def post_fault(self, envelope: dict[str, str]) -> tuple[int, Any]:
        """The agent's status and JSON answer; status 0 if it could not be reached."""
        return self._send(
            urllib.request.Request(  # noqa: S310 - an http(s) URL from configuration
                f"{self.url}/faults",
                data=json.dumps(envelope).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "rai-worker"},
            )
        )

    def revert(self, fault_id: str) -> tuple[int, Any]:
        """Ask the agent to revert one fault (never signed: reverting is always allowed)."""
        return self._send(
            urllib.request.Request(  # noqa: S310 - an http(s) URL from configuration
                f"{self.url}/faults/{fault_id}",
                method="DELETE",
                headers={"User-Agent": "rai-worker"},
            )
        )

    def open_fault_ids(self) -> set[str] | None:
        """The ids of the faults the agent has open, or None if it did not answer.
        The worker asks while it holds a fault, to notice one that ended early."""
        status, body = self._send(
            urllib.request.Request(  # noqa: S310 - an http(s) URL from configuration
                f"{self.url}/faults", headers={"User-Agent": "rai-worker"}
            )
        )
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("faults"), list):
            return None
        return {str(f.get("fault_id")) for f in body["faults"] if isinstance(f, dict)}

    def _send(self, request: urllib.request.Request) -> tuple[int, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"error": raw.decode("utf-8", "replace")[:300]}
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            return 0, {"error": f"the chaos agent is unreachable: {reason}"}


@dataclass(frozen=True)
class FaultOutcome:
    status: Literal["injected", "refused", "error"]
    by: Literal["worker", "agent"]
    fault: dict[str, Any] | None = None  # the agent's record: fault_id, onset, expires_at...
    reasons: tuple[str, ...] = ()

    @property
    def onset(self) -> str | None:
        return None if self.fault is None else self.fault.get("onset")


def _answer(prepared: PreparedFault, status: int, body: Any) -> FaultOutcome:
    error = str(body.get("error", "")) if isinstance(body, dict) else ""
    if status == 201 and isinstance(body, dict) and body.get("onset"):
        return FaultOutcome("injected", "agent", body)
    if status == 409 and isinstance(body, dict) and isinstance(body.get("fault"), dict):
        fault: dict[str, Any] = body["fault"]
        request = fault.get("request") or {}
        mine = (request.get("run_id"), request.get("step_key")) == (
            str(prepared.run_id), prepared.step_key,
        )  # fmt: skip
        if mine and fault.get("onset"):  # a retry: the agent already injected it
            return FaultOutcome("injected", "agent", fault)
        if mine:
            return FaultOutcome("error", "agent", fault, (fault.get("error") or error,))
    if status in (403, 409, 422):
        return FaultOutcome("refused", "agent", None, (error or f"HTTP {status}",))
    return FaultOutcome("error", "agent", None, (error or f"HTTP {status}",))


def record(
    conn: Connection,
    run_id: UUID,
    step_key: str,
    spec: FaultSpec,
    outcome: FaultOutcome,
    test_id: UUID | None = None,
) -> None:
    """The onset, refusal or error as one observation of the run (threat R-03).

    ``ON CONFLICT DO NOTHING`` keeps it to one row per run, step and rule when a
    step is retried."""
    kind, rule, result = {
        "injected": ("metric", "fault.onset", "pass"),
        "refused": ("refused", "fault.refused", "skipped"),
        "error": ("note", "fault.error", "error"),
    }[outcome.status]
    fault = outcome.fault or {}
    observed = {
        "by": outcome.by, "reasons": list(outcome.reasons), "fault_id": fault.get("fault_id"),
        "onset": fault.get("onset"), "expires_at": fault.get("expires_at"),
        "signed_by": fault.get("signed_by"),
    }  # fmt: skip
    conn.execute(
        text(
            "INSERT INTO rai.observations (run_id, step_key, test_id, kind, rule, observed_value, "
            "expected, outcome) VALUES (:run, :step, :test, CAST(:kind AS rai.observation_kind), "
            ":rule, CAST(:observed AS jsonb), CAST(:expected AS jsonb), "
            "CAST(:outcome AS rai.observation_outcome)) "
            "ON CONFLICT (run_id, step_key, rule) DO NOTHING"
        ),
        {
            "run": run_id, "step": step_key, "test": test_id, "kind": kind, "rule": rule,
            "observed": json.dumps(observed), "expected": spec.model_dump_json(),
            "outcome": result,
        },
    )  # fmt: skip


def record_revert(
    conn: Connection, run_id: UUID, step_key: str, spec: FaultSpec, status: int, body: Any
) -> None:
    """The agent's answer to the worker's revert, as an observation (B-26). With the
    onset, it bounds the fault in time; recovery is measured from these (FR-08.1)."""
    fault = body if isinstance(body, dict) else {}
    done = status == 200 and fault.get("status") == "reverted"
    observed = {
        "fault_id": fault.get("fault_id"), "reverted_at": fault.get("reverted_at"),
        "reverted_by": fault.get("reverted_by"), "status": status,
        "error": None if done else (fault.get("error") or str(body)[:300]),
    }  # fmt: skip
    conn.execute(
        text(
            "INSERT INTO rai.observations (run_id, step_key, kind, rule, observed_value, "
            "expected, outcome) VALUES (:run, :step, CAST(:kind AS rai.observation_kind), "
            "'fault.reverted', CAST(:observed AS jsonb), CAST(:expected AS jsonb), "
            "CAST(:outcome AS rai.observation_outcome)) "
            "ON CONFLICT (run_id, step_key, rule) DO NOTHING"
        ),
        {
            "run": run_id, "step": step_key, "kind": "metric" if done else "note",
            "observed": json.dumps(observed), "expected": spec.model_dump_json(),
            "outcome": "pass" if done else "error",
        },
    )  # fmt: skip


class ChaosClient:
    """Preflight, sign, send, record: how a scenario step asks for one fault."""

    def __init__(self, signer: Signer, agent: AgentClient) -> None:
        self.signer, self.agent = signer, agent

    def inject(
        self,
        conn: Connection,
        pins: Pins,
        spec: FaultSpec,
        *,
        run_id: UUID,
        tenant_id: UUID,
        step_key: str,
        test_id: UUID | None = None,
    ) -> FaultOutcome:
        prepared, problems = fault_preflight(
            conn, pins, spec, run_id=run_id, tenant_id=tenant_id, step_key=step_key
        )
        if prepared is None:
            outcome = FaultOutcome("refused", "worker", None, tuple(problems))
            record(conn, run_id, step_key, spec, outcome, test_id)
            return outcome
        status, body = self.agent.post_fault(self.signer.sign(prepared.request()))
        outcome = _answer(prepared, status, body)
        record(conn, run_id, step_key, spec, outcome, test_id)
        return outcome


def from_env(environ: dict[str, str]) -> ChaosClient | None:
    """The client, if this worker is configured for faults; ``None`` if it is not."""
    ref, url = environ.get(SIGNING_KEY_ENV), environ.get(AGENT_URL_ENV)
    if not ref and not url:
        return None
    if not (ref and url):
        raise ChaosSetupError(f"set both {SIGNING_KEY_ENV} and {AGENT_URL_ENV}, or neither")
    try:
        signer = Signer.from_ref(ref)
    except ValueError as error:  # SecretError, or a key that does not parse
        raise ChaosSetupError(f"{SIGNING_KEY_ENV}: {error}") from None
    return ChaosClient(signer, AgentClient(url))
