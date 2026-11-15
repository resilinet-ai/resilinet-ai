"""Runs: states and the pins a run carries (B-18: FR-06.1, FR-06.2, NFR-REP-1).

A run pins every input when it is queued: the plan version and, for each item,
the test-case version (with its definition), the target asset and the contract
version with its SHA-256. The worker executes from the pins alone, so a run means
the same thing whenever it executes, and a re-run can be compared exactly.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

RunState = Literal[
    "queued", "preflight", "running", "evaluating", "passed", "failed", "refused", "aborted",
    "error",
]  # fmt: skip
TERMINAL: frozenset[str] = frozenset({"passed", "failed", "refused", "aborted", "error"})
PINS_FORMAT: Literal["rai-run-pins/1"] = "rai-run-pins/1"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PinnedPlan(_Frozen):
    plan_id: UUID
    name: str
    version: int


class PinnedTest(_Frozen):
    test_id: UUID
    name: str
    version: int
    type: str
    severity: str
    definition: dict[str, Any]


class PinnedAsset(_Frozen):
    asset_id: UUID
    name: str
    locator: str | None


class PinnedContract(_Frozen):
    contract_id: UUID
    ref: str
    spec_sha256: str


class PinnedItem(_Frozen):
    position: int
    step_key: str
    test: PinnedTest
    asset: PinnedAsset | None = None
    contract: PinnedContract | None = None


class Pins(_Frozen):
    format: Literal["rai-run-pins/1"] = PINS_FORMAT
    rai_version: str
    plan: PinnedPlan
    items: list[PinnedItem] = Field(min_length=1)


def step_key(position: int) -> str:
    return f"item-{position}"
