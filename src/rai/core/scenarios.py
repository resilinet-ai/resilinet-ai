"""Chaos scenarios: the faults a scenario declares (B-26: FR-07.1, FR-07.2; B-24 FaultSpec).

Shared by the API (which validates a scenario when it is published), the run pins,
and the worker (which signs one request per fault). A scenario declares its blast
radius once, every target it may touch, and its faults in order; each fault's
target must be in the radius.
"""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text

FaultType = Literal[
    "latency", "timeout", "connection_drop", "network_partition", "container_stop",
    "container_restart", "stale_upstream",
]  # fmt: skip
PROXY_FAULTS = frozenset({"latency", "timeout", "connection_drop"})
TARGET_NAME = r"^[a-z0-9][a-z0-9_.-]{0,62}$"
MAX_FAULTS = 20
MAX_BLAST_RADIUS = 20


class ScenarioFault(BaseModel):
    """One fault of a scenario, as its author declares it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: FaultType
    target: str = Field(pattern=TARGET_NAME)
    duration_seconds: int = Field(ge=1, le=3600)
    latency_ms: int | None = Field(default=None, ge=1, le=60_000)
    jitter_ms: int = Field(default=0, ge=0, le=60_000)
    network: str | None = Field(default=None, pattern=TARGET_NAME)

    @model_validator(mode="after")
    def _complete(self) -> ScenarioFault:
        if self.type == "latency" and self.latency_ms is None:
            raise ValueError("latency needs latency_ms")
        if self.type == "network_partition" and self.network is None:
            raise ValueError("network_partition needs the network to disconnect from")
        return self


class FaultSpec(ScenarioFault):
    """One fault with the scenario's blast radius: what the worker checks and signs."""

    blast_radius: tuple[str, ...] = Field(min_length=1, max_length=MAX_BLAST_RADIUS)

    @model_validator(mode="after")
    def _inside(self) -> FaultSpec:
        if self.target not in self.blast_radius:
            raise ValueError(f"target {self.target!r} is outside the declared blast radius")
        return self


class ScenarioDefinition(BaseModel):
    """What a scenario version holds: its blast radius and its faults, in order."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    blast_radius: tuple[str, ...] = Field(min_length=1, max_length=MAX_BLAST_RADIUS)
    faults: tuple[ScenarioFault, ...] = Field(min_length=1, max_length=MAX_FAULTS)

    @model_validator(mode="after")
    def _within_radius(self) -> ScenarioDefinition:
        for name in self.blast_radius:
            if not _is_name(name):
                raise ValueError(f"blast_radius: {name!r} is not a target name")
        if len(set(self.blast_radius)) != len(self.blast_radius):
            raise ValueError("blast_radius lists a target twice")
        for n, fault in enumerate(self.faults):
            if fault.target not in self.blast_radius:
                raise ValueError(
                    f"faults[{n}]: target {fault.target!r} is outside the declared blast radius"
                )
        return self

    def specs(self) -> list[FaultSpec]:
        radius = tuple(sorted(self.blast_radius))
        return [FaultSpec(**f.model_dump(), blast_radius=radius) for f in self.faults]


def _is_name(name: str) -> bool:
    return re.fullmatch(TARGET_NAME, name) is not None


class Decision(BaseModel):
    """The latest review decision on a scenario version (B-26: R-02)."""

    model_config = ConfigDict(frozen=True)
    approval_id: UUID
    decision: Literal["approve", "reject"]
    reviewer_id: UUID


def latest_decision(conn: Connection, scenario_id: UUID) -> Decision | None:
    """The newest decision on the version, or None if nobody has reviewed it. Only an
    ``approve`` here makes the version runnable; a later ``reject`` withdraws it."""
    row = conn.execute(
        text(
            "SELECT approval_id, decision::text AS decision, reviewer_id "
            "FROM rai.scenario_approvals WHERE scenario_id = :s "
            "ORDER BY created_at DESC, approval_id DESC LIMIT 1"
        ),
        {"s": scenario_id},
    ).first()
    return None if row is None else Decision.model_validate(row._asdict())
