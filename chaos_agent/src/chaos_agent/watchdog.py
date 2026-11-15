"""The independent watchdog (B-25: threat D-01; FR-07.3).

A second process, in its own container, that reverts any fault past its deadline
whatever happened to the agent: crashed, killed, hung, or stuck halfway through an
injection. It shares only the state file (one volume) and the two backends (on the
internal ``chaos_net``). It has no HTTP interface, so nothing can ask it to inject;
it can only make the sandbox healthier.

**Deadline.** ``requested_at + duration + grace`` for every open fault, including
one still marked ``injecting`` (the agent died mid-injection and set no expiry).
The grace (``RAI_WATCHDOG_GRACE_SECONDS``, default 30) leaves the agent's own expiry
loop to act first in normal running; the watchdog is the backstop.

**Locking.** It takes the same cross-process lock as the agent, so it never undoes
an injection in progress. If the lock is not free within ``RAI_WATCHDOG_LOCK_WAIT``
seconds (default 20: longer than any backend call), the agent is presumed hung and
the watchdog reverts overdue faults without the lock: every revert is idempotent,
and a revert only ever makes the sandbox healthier. Without the lock it leaves the
state file alone; the agent re-reverts (harmlessly) and records it when it recovers.

**Liveness.** Each sweep touches ``<state dir>/watchdog.heartbeat``; the container's
health check fails if it is older than a minute.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from chaos_agent.agent import OPEN, Agent
from chaos_agent.clients import BackendError
from chaos_agent.faults import Fault, LockTimeout, now, stamp

log = logging.getLogger("rai.chaos_watchdog")
DEFAULT_GRACE_SECONDS = 30
DEFAULT_LOCK_WAIT = 20.0
DEFAULT_INTERVAL = 5.0
HEARTBEAT = "watchdog.heartbeat"


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


def deadline(fault: Fault, grace: timedelta) -> datetime:
    """When the watchdog steps in: from the request, so an unfinished injection counts."""
    return _parse(fault.requested_at) + timedelta(seconds=fault.request.duration_seconds) + grace


class Watchdog:
    def __init__(
        self,
        agent: Agent,
        *,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        lock_wait: float = DEFAULT_LOCK_WAIT,
    ) -> None:
        self.agent, self.state = agent, agent.state
        self.grace = timedelta(seconds=grace_seconds)
        self.lock_wait = lock_wait

    def overdue(self, faults: dict[str, Fault], moment: datetime) -> list[Fault]:
        return [
            f for f in faults.values() if f.status in OPEN and deadline(f, self.grace) <= moment
        ]

    def sweep(self, moment: datetime | None = None) -> list[Fault]:
        """Revert every overdue fault; returns those reverted. Never raises for a backend
        failure: the next sweep tries again."""
        moment = moment or now()
        try:
            with self.state.locked(timeout=self.lock_wait):
                reverted = []
                for fault in self.overdue(self.state.load(), moment):
                    if self._undo(fault):
                        fault.status, fault.reverted_at = "reverted", stamp(now())
                        fault.reverted_by = "watchdog"
                        self.state.put(fault)
                        log.warning("reverted overdue fault %s (%s on %s)", fault.fault_id,
                                    fault.request.type, fault.request.target)  # fmt: skip
                        reverted.append(fault)
                return reverted
        except LockTimeout:
            # The agent holds the state far longer than any injection takes: hung. Revert
            # without the lock (idempotent) and leave the record to the agent.
            overdue = self.overdue(self.state.load(), moment)
            log.error("state lock held over %.0f s; reverting %d overdue fault(s) without it",
                      self.lock_wait, len(overdue))  # fmt: skip
            return [fault for fault in overdue if self._undo(fault)]

    def _undo(self, fault: Fault) -> bool:
        try:
            self.agent._undo(fault)  # exactly the revert the agent itself performs
        except BackendError:
            log.exception("could not revert %s; trying again next sweep", fault.fault_id)
            return False
        return True

    def heartbeat(self) -> None:
        (self.state.path.parent / HEARTBEAT).write_text(stamp(now()), encoding="utf-8")

    def run(self, stop: threading.Event, interval: float = DEFAULT_INTERVAL) -> None:
        log.info("watchdog: grace %s, lock wait %.0f s, every %.0f s", self.grace,
                 self.lock_wait, interval)  # fmt: skip
        while True:
            try:
                self.sweep()
                self.heartbeat()
            except Exception:  # keep watching whatever one sweep does
                log.exception("watchdog sweep failed")
            if stop.wait(interval):
                return


def settings(environ: dict[str, str] | None = None) -> tuple[int, float, float]:
    env = os.environ if environ is None else environ
    return (
        int(env.get("RAI_WATCHDOG_GRACE_SECONDS", DEFAULT_GRACE_SECONDS)),
        float(env.get("RAI_WATCHDOG_LOCK_WAIT", DEFAULT_LOCK_WAIT)),
        float(env.get("RAI_WATCHDOG_INTERVAL", DEFAULT_INTERVAL)),
    )


def healthy(state_dir: Path, max_age: timedelta = timedelta(minutes=1)) -> bool:
    """For the container health check: the last sweep finished recently."""
    try:
        beat = _parse((state_dir / HEARTBEAT).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return now() - beat <= max_age
