"""Fault requests and records (B-23: FR-07.1; Protocol section 5).

Seven fault types, exactly the protocol's (scenarios S01 to S20 combine them):

=================  ===========  =========================================  ============
type               target kind  mechanism                                  revert
=================  ===========  =========================================  ============
latency            proxy        Toxiproxy ``latency`` toxic (downstream)   remove toxic
timeout            proxy        Toxiproxy ``timeout`` toxic, 0 ms: calls   remove toxic
                                hang until the client gives up
connection_drop    proxy        disable the Toxiproxy proxy: open          enable proxy
                                connections close, new ones are refused
network_partition  container    Docker network disconnect                  reconnect,
                                                                           same aliases
container_stop     container    Docker stop                                start
container_restart  container    Docker restart                             (instant)
stale_upstream     container    Docker pause of the producer: its output   unpause
                                stops while it holds its state
=================  ===========  =========================================  ============
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

FaultType = Literal[
    "latency", "timeout", "connection_drop", "network_partition", "container_stop",
    "container_restart", "stale_upstream",
]  # fmt: skip
FAULT_TYPES: tuple[str, ...] = (
    "latency", "timeout", "connection_drop", "network_partition", "container_stop",
    "container_restart", "stale_upstream",
)  # fmt: skip
PROXY_FAULTS = frozenset({"latency", "timeout", "connection_drop"})
INSTANT_FAULTS = frozenset({"container_restart"})
NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
LABEL = re.compile(r"^[a-z0-9][a-z0-9_./-]{0,99}=[A-Za-z0-9_.-]{1,100}$")
HARD_MAX_SECONDS = 3600  # whatever the request says (environment ceilings are at most 60 min)
MAX_BLAST_RADIUS = 20
SCENARIO_STEP = re.compile(r"^[A-Za-z0-9_-]{1,80}\.fault-[1-9][0-9]{0,2}$")

Status = Literal["injecting", "active", "reverted", "failed"]


class FaultError(ValueError):
    """A request the agent refuses; the message says why."""


def now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class FaultRequest:
    type: str
    target: str
    duration_seconds: int
    required_label: str | None = None  # the label the target registered with (B-22)
    latency_ms: int | None = None
    jitter_ms: int = 0
    network: str | None = None  # network_partition: the network to disconnect from
    run_id: str | None = None
    max_duration_seconds: int = HARD_MAX_SECONDS  # the environment's ceiling
    # B-24: who asked, where, and how far the scenario may reach (signed by the worker).
    step_key: str | None = None
    tenant_id: str | None = None
    environment: str | None = None
    blast_radius: tuple[str, ...] = ()  # every target the scenario declares
    # B-27: the scenario version ("<scenario_id>@<version>"); its faults share one radius.
    scenario: str | None = None

    @classmethod
    def parse(cls, data: Any) -> FaultRequest:
        if not isinstance(data, dict):
            raise FaultError("the request must be a JSON object")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(data) - known)
        if unknown:
            raise FaultError(f"unknown fields: {', '.join(unknown)}")
        if not isinstance(data.get("blast_radius", []), list | tuple):
            raise FaultError("blast_radius must be a list of target names")
        try:
            request = cls(**(data | {"blast_radius": tuple(data.get("blast_radius", ()))}))
        except TypeError as error:
            raise FaultError(f"missing fields: {error}") from None
        request.check()
        return request

    def check(self) -> None:
        if self.type not in FAULT_TYPES:
            raise FaultError(f"type must be one of {', '.join(FAULT_TYPES)}")
        if not isinstance(self.target, str) or not NAME.fullmatch(self.target):
            raise FaultError("target must be a registered target name")
        ceiling = min(int(self.max_duration_seconds), HARD_MAX_SECONDS)
        if not isinstance(self.duration_seconds, int) or not 1 <= self.duration_seconds <= ceiling:
            raise FaultError(f"duration_seconds must be 1 to {ceiling} (the environment ceiling)")
        if self.required_label is not None and not LABEL.fullmatch(self.required_label):
            raise FaultError("required_label must look like key=value")
        if self.type == "latency" and not (
            isinstance(self.latency_ms, int) and 1 <= self.latency_ms <= 60_000
        ):
            raise FaultError("latency needs latency_ms from 1 to 60000")
        if not isinstance(self.jitter_ms, int) or not 0 <= self.jitter_ms <= 60_000:
            raise FaultError("jitter_ms must be 0 to 60000")
        if self.type == "network_partition" and not (
            isinstance(self.network, str) and NAME.fullmatch(self.network)
        ):
            raise FaultError("network_partition needs the network to disconnect from")
        if self.type not in PROXY_FAULTS and self.required_label is None:
            raise FaultError("container faults need the target's required_label")
        if not 1 <= len(self.blast_radius) <= MAX_BLAST_RADIUS or not all(
            isinstance(name, str) and NAME.fullmatch(name) for name in self.blast_radius
        ):
            raise FaultError(f"blast_radius must list 1 to {MAX_BLAST_RADIUS} target names")
        if self.target not in self.blast_radius:
            raise FaultError(f"target {self.target!r} is outside the declared blast radius")
        for name, value in (
            ("run_id", self.run_id),
            ("step_key", self.step_key),
            ("tenant_id", self.tenant_id),
            ("environment", self.environment),
            ("scenario", self.scenario),
        ):
            if value is not None and not (isinstance(value, str) and 0 < len(value) <= 100):
                raise FaultError(f"{name} must be text of at most 100 characters")  # fmt: skip
        if self.scenario is not None and (
            self.run_id is None or self.step_key is None or not SCENARIO_STEP.fullmatch(
                self.step_key
            )
        ):  # fmt: skip
            raise FaultError("a scenario fault needs run_id and a step_key like item-1.fault-2")

    @property
    def step_scope(self) -> tuple[str, str] | None:
        """(run, step) without the fault number: ``item-1.fault-2`` is step ``item-1``."""
        if self.run_id is None or self.step_key is None:
            return None
        return self.run_id, self.step_key.rsplit(".fault-", 1)[0]

    @property
    def scenario_step(self) -> tuple[str, str] | None:
        """The step scope of a scenario fault; every fault of it keeps one radius."""
        return None if self.scenario is None else self.step_scope


@dataclass
class Fault:
    """One fault, as persisted in the state file before and after it acts."""

    request: FaultRequest
    fault_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: Status = "injecting"
    requested_at: str = field(default_factory=lambda: stamp(now()))
    onset: str | None = None  # t0: when the fault took effect (Architecture section 4)
    expires_at: str | None = None
    reverted_at: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)  # what the revert needs
    error: str | None = None
    signed_by: str | None = None  # the worker key that signed the request (B-24)
    # B-25: who reverted it: request, expiry, kill, startup or watchdog.
    reverted_by: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Fault:
        request = data["request"]
        request = request | {"blast_radius": tuple(request.get("blast_radius", ()))}
        return cls(**{**data, "request": FaultRequest(**request)})


class LockTimeout(TimeoutError):
    """Another process has held the state lock for longer than we may wait."""


class StateFile:
    """Active faults on local disk, written before the agent acts (threat R-03), so a
    revert works after a crash and with the API and database down.

    B-25: the agent and the watchdog are separate processes (separate containers on
    one volume), so :meth:`locked` takes an advisory lock on ``<state>.lock`` as well
    as a thread lock. The lock is held for a whole check-persist-act sequence and is
    released by the kernel if its holder dies.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._depth = 0  # re-entry within the thread that holds the file lock
        self._handle: Any = None
        path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self, timeout: float | None = None) -> Iterator[None]:
        """Hold the state for one sequence of reads and writes, across processes.
        Raises :class:`LockTimeout` if it cannot be had within ``timeout`` seconds."""
        if not self._lock.acquire(timeout=-1 if timeout is None else timeout):
            raise LockTimeout("another thread holds the state")
        try:
            if self._depth == 0:
                self._handle = _acquire(self.path.with_name(self.path.name + ".lock"), timeout)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    _release(self._handle)
                    self._handle = None
        finally:
            self._lock.release()

    def load(self) -> dict[str, Fault]:
        with self._lock:
            if not self.path.exists():
                return {}
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            return {k: Fault.from_json(v) for k, v in data.get("faults", {}).items()}

    def save(self, faults: dict[str, Fault]) -> None:
        """Atomically: a crash leaves the old file or the new one, never half of one."""
        with self._lock:
            records = {k: f.to_json() for k, f in faults.items()}
            payload = json.dumps(
                {"format": "rai-chaos-state/1", "faults": records}, indent=2, sort_keys=True
            )
            handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            Path(temp).replace(self.path)

    def put(self, fault: Fault) -> None:
        with self._lock:
            faults = self.load()
            faults[fault.fault_id] = fault
            self.save(faults)


# ---- the cross-process lock: flock on Linux (the containers), msvcrt for local tests ---------


def _acquire(path: Path, timeout: float | None) -> Any:
    handle = path.open("a+b")
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            _try_lock(handle)
            return handle
        except OSError:
            if deadline is not None and time.monotonic() >= deadline:
                handle.close()
                raise LockTimeout(f"{path} is held by another process") from None
            time.sleep(0.05)


def _release(handle: Any) -> None:
    try:
        _unlock(handle)
    finally:
        handle.close()


if sys.platform == "win32":  # pragma: no cover - development on Windows
    import msvcrt

    def _try_lock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
