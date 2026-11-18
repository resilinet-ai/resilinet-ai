"""The chaos agent: the only component that injects faults (B-23: FR-07.1; NFR-SAF-1).

Before acting, the agent checks on its own (it never trusts the caller alone):

* the fault type is one of the seven, and the duration is within the environment's
  ceiling and the agent's hard limit of 60 minutes;
* a **container** target is exactly one container of that Compose service **in the
  agent's own Compose project** (read from its own container's labels; if it cannot
  tell, container faults are refused), carrying ``rai.sandbox=true`` and the label the
  target registered with;
* a **proxy** target exists in Toxiproxy (which is reachable only on ``chaos_net``);
* the target has no other active fault.

B-24 adds the agent's half of the second preflight (threats S-03, D-02), on top of
the signature checked in :mod:`chaos_agent.signing`:

* **replay guard:** one fault per run and step, ever; a repeat is refused with the
  fault already recorded for it (so a worker that retries learns the onset);
* the request is for the agent's own environment;
* the duration is within the agent's own ceiling (``RAI_AGENT_MAX_FAULT_SECONDS``),
  whatever the signed environment ceiling says;
* **blast radius:** the target is in the radius the scenario declared, a run keeps
  the radius it started with, and at most ``RAI_AGENT_MAX_OPEN_FAULTS`` faults are
  open at once.

B-27 makes the radius **scenario-level** (threat D-02): a request naming a scenario
version is held to every fault already recorded for the same run and scenario step,
open *or reverted*. They must name the same scenario version and declare the same
radius, so a scenario cannot widen its reach between faults, after the first one was
reverted.

Every fault is written to the state file **before** it acts, then marked active
with its onset (t0), or failed. Faults past their duration are reverted by the
expiry loop; faults found active when the agent starts (it crashed) are reverted
first. The agent and the independent watchdog (:mod:`chaos_agent.watchdog`, B-25) share
one cross-process lock on the state file, and every revert is idempotent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from chaos_agent.clients import BackendError, Docker, Toxiproxy
from chaos_agent.faults import (
    HARD_MAX_SECONDS,
    INSTANT_FAULTS,
    PROXY_FAULTS,
    Fault,
    FaultError,
    FaultRequest,
    StateFile,
    now,
    stamp,
)

log = logging.getLogger("rai.chaos_agent")
SANDBOX_LABEL = ("rai.sandbox", "true")
OPEN = ("injecting", "active")
DEFAULT_MAX_FAULT_SECONDS = 900  # the protocol's 15-minute environment default
DEFAULT_MAX_OPEN_FAULTS = 3


class FaultReplay(FaultError):
    """The run and step already had a fault: a replay or a worker retry (409)."""

    def __init__(self, fault: Fault) -> None:
        super().__init__(
            f"run {fault.request.run_id} step {fault.request.step_key} already has fault "
            f"{fault.fault_id} ({fault.status}); a signed request is used once"
        )
        self.fault = fault


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


class Agent:
    def __init__(
        self,
        state: StateFile,
        toxiproxy: Toxiproxy,
        docker: Docker,
        project: str | None,
        *,
        environment: str | None = None,
        max_fault_seconds: int = DEFAULT_MAX_FAULT_SECONDS,
        max_open_faults: int = DEFAULT_MAX_OPEN_FAULTS,
    ) -> None:
        self.state, self.toxiproxy, self.docker = state, toxiproxy, docker
        self.project = project  # the agent's own Compose project: the only one it touches
        self.environment = environment  # the one environment it serves (B-24)
        self.max_fault_seconds = min(max_fault_seconds, HARD_MAX_SECONDS)
        self.max_open_faults = max_open_faults

    # ---- checks ----------------------------------------------------------------------------

    def _container(self, request: FaultRequest) -> dict[str, Any]:
        if self.project is None:
            raise FaultError("the agent could not tell its own Compose project; container "
                             "faults are refused")  # fmt: skip
        found = self.docker.find(request.target, self.project)
        if len(found) != 1:
            raise FaultError(
                f"target {request.target!r} must be exactly one container (found {len(found)})"
            )
        labels: dict[str, str] = found[0].get("Labels") or {}
        if labels.get(SANDBOX_LABEL[0]) != SANDBOX_LABEL[1]:
            raise FaultError(f"target {request.target!r} is not labelled rai.sandbox=true")
        key, _, value = (request.required_label or "").partition("=")
        if labels.get(key) != value:
            raise FaultError(f"target {request.target!r} does not carry {request.required_label}")
        return found[0]

    def _second_preflight(self, request: FaultRequest, faults: dict[str, Fault]) -> None:
        """The agent's own limits, whatever the (signed) request says (S-03, D-02)."""
        if request.run_id is not None and request.step_key is not None:
            for other in faults.values():
                if (other.request.run_id, other.request.step_key) == (
                    request.run_id, request.step_key,
                ):  # fmt: skip
                    raise FaultReplay(other)
        if self.environment is not None and request.environment != self.environment:
            raise FaultError(f"this agent serves environment {self.environment!r} only")
        if request.duration_seconds > self.max_fault_seconds:
            raise FaultError(
                f"duration_seconds exceeds this agent's ceiling of {self.max_fault_seconds} s"
            )
        open_ = [f for f in faults.values() if f.status in OPEN]
        if len(open_) >= self.max_open_faults:
            raise FaultError(f"{len(open_)} faults are open; this agent allows "
                             f"{self.max_open_faults} at once")  # fmt: skip
        step = request.scenario_step
        if step is not None:  # B-27: every fault of the scenario step, open or not
            for other in faults.values():
                if other.request.step_scope != step:
                    continue
                if other.request.scenario != request.scenario:
                    raise FaultError(f"step {step[1]!r} of this run already ran scenario "
                                     f"{other.request.scenario!r}")  # fmt: skip
                if set(other.request.blast_radius) != set(request.blast_radius):
                    raise FaultError("a scenario keeps the blast radius it declared with its "
                                     "first fault, open or reverted")  # fmt: skip
        for other in open_:
            if (
                request.run_id is not None
                and other.request.run_id == request.run_id
                and set(other.request.blast_radius) != set(request.blast_radius)
            ):
                raise FaultError("a run keeps the blast radius it declared with its first fault")

    def _check(self, request: FaultRequest) -> dict[str, Any]:
        """What the revert will need; refuses (FaultError) if anything is off."""
        faults = self.state.load()
        self._second_preflight(request, faults)
        for other in faults.values():
            if other.status in OPEN and other.request.target == request.target:
                raise FaultError(f"target {request.target!r} already has an active fault")
        if request.type in PROXY_FAULTS:
            if self.toxiproxy.proxy(request.target) is None:
                raise FaultError(f"proxy {request.target!r} does not exist in Toxiproxy")
            return {"proxy": request.target, "toxic": None}
        container = self._container(request)
        detail: dict[str, Any] = {"container_id": container["Id"]}
        if request.type == "network_partition":
            networks = self.docker.inspect(container["Id"])["NetworkSettings"]["Networks"]
            matches = [n for n in networks if n == request.network
                       or n.endswith(f"_{request.network}")]  # fmt: skip
            if len(matches) != 1:
                raise FaultError(f"target {request.target!r} is not on network {request.network!r}")
            detail |= {"network": matches[0], "aliases": networks[matches[0]].get("Aliases") or []}
        return detail

    # ---- acting ----------------------------------------------------------------------------

    def _apply(self, fault: Fault) -> None:
        request, detail = fault.request, fault.detail
        if request.type in ("latency", "timeout"):
            name = detail["toxic"]  # named before the fault was recorded
            attributes = (
                {"latency": request.latency_ms, "jitter": request.jitter_ms}
                if request.type == "latency" else {"timeout": 0}
            )  # fmt: skip
            self.toxiproxy.add_toxic(request.target, {
                "name": name, "type": request.type, "stream": "downstream", "toxicity": 1.0,
                "attributes": attributes,
            })  # fmt: skip
        elif request.type == "connection_drop":
            self.toxiproxy.set_enabled(request.target, False)
        elif request.type == "network_partition":
            self.docker.disconnect(detail["network"], detail["container_id"])
        else:
            verb = {"container_stop": "stop", "container_restart": "restart",
                    "stale_upstream": "pause"}[request.type]  # fmt: skip
            self.docker.action(detail["container_id"], verb)

    def _undo(self, fault: Fault) -> None:
        request, detail = fault.request, fault.detail
        if request.type in ("latency", "timeout"):
            if detail.get("toxic"):
                self.toxiproxy.remove_toxic(request.target, detail["toxic"])
        elif request.type == "connection_drop":
            self.toxiproxy.set_enabled(request.target, True)
        elif request.type == "network_partition":
            self.docker.connect(detail["network"], detail["container_id"], detail["aliases"])
        elif request.type == "container_stop":
            self.docker.action(detail["container_id"], "start")
        elif request.type == "stale_upstream":
            self.docker.action(detail["container_id"], "unpause")
        # container_restart: nothing to undo

    def inject(self, request: FaultRequest, *, signed_by: str | None = None) -> Fault:
        """Check, persist, act; returns the fault with its onset (t0)."""
        with self.state.locked():  # across processes: the watchdog waits (B-25)
            fault = Fault(request=request, detail=self._check(request), signed_by=signed_by)
            if request.type in ("latency", "timeout"):
                # Named now, so a revert after a crash mid-injection can find the toxic.
                fault.detail["toxic"] = f"rai-{fault.fault_id[:8]}"
            self.state.put(fault)  # recorded before anything changes (R-03)
            try:
                self._apply(fault)
            except BackendError as error:
                fault.status, fault.error = "failed", str(error)[:500]
                try:  # half-applied faults are undone at once
                    self._undo(fault)
                except BackendError:
                    log.exception("could not undo failed fault %s", fault.fault_id)
                self.state.put(fault)
                raise
            onset = now()
            fault.onset = stamp(onset)
            if request.type in INSTANT_FAULTS:
                fault.status, fault.reverted_at = "reverted", fault.onset
            else:
                fault.status = "active"
                fault.expires_at = stamp(onset + timedelta(seconds=request.duration_seconds))
            self.state.put(fault)
            log.info("fault %s %s on %s at %s", fault.fault_id, request.type, request.target,
                     fault.onset)  # fmt: skip
            return fault

    def revert(
        self, fault_id: str, *, expired_by: datetime | None = None, by: str = "request"
    ) -> Fault:
        """Revert one fault. With ``expired_by``, only if it has expired by then (checked
        under the lock, so an injection in progress is never undone by the expiry loop)."""
        with self.state.locked():
            fault = self.state.load().get(fault_id)
            if fault is None:
                raise KeyError(fault_id)
            if expired_by is not None and (
                fault.expires_at is None or _parse(fault.expires_at) > expired_by
            ):
                return fault
            if fault.status in OPEN:
                self._undo(fault)
                fault.status, fault.reverted_at, fault.reverted_by = "reverted", stamp(now()), by
                self.state.put(fault)
                log.info("fault %s reverted (%s)", fault_id, by)
            return fault

    def open_faults(self) -> list[Fault]:
        return [f for f in self.state.load().values() if f.status in OPEN]

    def revert_all(self, *, by: str = "kill") -> list[Fault]:
        """Revert every open fault (kill --all); keeps going past failures."""
        reverted = []
        for fault in self.open_faults():
            try:
                reverted.append(self.revert(fault.fault_id, by=by))
            except BackendError:
                log.exception("could not revert %s", fault.fault_id)
        return reverted

    def revert_expired(self) -> list[Fault]:
        """Faults past their duration. A fault still being injected has no expiry yet and
        is left alone; one left half-done by a crash is reverted at start-up instead."""
        moment = now()
        reverted = []
        for fault in self.open_faults():
            if fault.expires_at is not None and _parse(fault.expires_at) <= moment:
                done = self.revert(fault.fault_id, expired_by=moment, by="expiry")
                if done.status == "reverted":
                    reverted.append(done)
        return reverted
