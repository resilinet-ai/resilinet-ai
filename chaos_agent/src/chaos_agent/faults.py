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
import tempfile
import threading
import uuid
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

    @classmethod
    def parse(cls, data: Any) -> FaultRequest:
        if not isinstance(data, dict):
            raise FaultError("the request must be a JSON object")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(data) - known)
        if unknown:
            raise FaultError(f"unknown fields: {', '.join(unknown)}")
        try:
            request = cls(**data)
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

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Fault:
        return cls(**{**data, "request": FaultRequest(**data["request"])})


class StateFile:
    """Active faults on local disk, written before the agent acts (threat R-03), so a
    revert works after a crash and with the API and database down."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)

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
