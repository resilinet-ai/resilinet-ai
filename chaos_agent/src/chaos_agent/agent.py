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

Every fault is written to the state file **before** it acts, then marked active
with its onset (t0), or failed. Faults past their duration are reverted by the
expiry loop; faults found active when the agent starts (it crashed) are reverted
first. Signed requests and the second preflight come with B-24; the watchdog and
the kill-switch drill with B-25.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from chaos_agent.clients import BackendError, Docker, Toxiproxy
from chaos_agent.faults import (
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


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


class Agent:
    def __init__(
        self, state: StateFile, toxiproxy: Toxiproxy, docker: Docker, project: str | None
    ) -> None:
        self.state, self.toxiproxy, self.docker = state, toxiproxy, docker
        self.project = project  # the agent's own Compose project: the only one it touches
        self._lock = threading.Lock()

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

    def _check(self, request: FaultRequest) -> dict[str, Any]:
        """What the revert will need; refuses (FaultError) if anything is off."""
        for other in self.state.load().values():
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
            name = f"rai-{fault.fault_id[:8]}"
            attributes = (
                {"latency": request.latency_ms, "jitter": request.jitter_ms}
                if request.type == "latency" else {"timeout": 0}
            )  # fmt: skip
            self.toxiproxy.add_toxic(request.target, {
                "name": name, "type": request.type, "stream": "downstream", "toxicity": 1.0,
                "attributes": attributes,
            })  # fmt: skip
            detail["toxic"] = name
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

    def inject(self, request: FaultRequest) -> Fault:
        """Check, persist, act; returns the fault with its onset (t0)."""
        with self._lock:
            fault = Fault(request=request, detail=self._check(request))
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

    def revert(self, fault_id: str, *, expired_by: datetime | None = None) -> Fault:
        """Revert one fault. With ``expired_by``, only if it has expired by then (checked
        under the lock, so an injection in progress is never undone by the expiry loop)."""
        with self._lock:
            fault = self.state.load().get(fault_id)
            if fault is None:
                raise KeyError(fault_id)
            if expired_by is not None and (
                fault.expires_at is None or _parse(fault.expires_at) > expired_by
            ):
                return fault
            if fault.status in OPEN:
                self._undo(fault)
                fault.status, fault.reverted_at = "reverted", stamp(now())
                self.state.put(fault)
                log.info("fault %s reverted", fault_id)
            return fault

    def open_faults(self) -> list[Fault]:
        return [f for f in self.state.load().values() if f.status in OPEN]

    def revert_all(self) -> list[Fault]:
        """Revert every open fault (kill --all); keeps going past failures."""
        reverted = []
        for fault in self.open_faults():
            try:
                reverted.append(self.revert(fault.fault_id))
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
                done = self.revert(fault.fault_id, expired_by=moment)
                if done.status == "reverted":
                    reverted.append(done)
        return reverted
