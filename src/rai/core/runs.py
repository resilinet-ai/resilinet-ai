"""Runs: states and the pins a run carries (B-18: FR-06.1, FR-06.2, NFR-REP-1; B-19: FR-04.1;
B-26: scenario items).

A run pins every input when it is queued: the plan version and, for each item,
the test-case version (with its definition), the target asset, the contract
version with its SHA-256, and the target's declared upstream graph. The worker
executes from the pins alone, so a run means the same thing whenever it executes,
and a re-run can be compared exactly.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rai.core.scenarios import ScenarioFault

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
    kind: str | None = None  # B-19; absent in pins written by B-18


class PinnedContract(_Frozen):
    contract_id: UUID
    ref: str
    spec_sha256: str


class PinnedInput(_Frozen):
    """One declared hop of the target's upstream graph: ``source`` feeds ``into``."""

    source: PinnedAsset
    into: UUID
    transform_ref: str | None = None


class PinnedScenario(_Frozen):
    """A chaos scenario version (B-26) and the approval it ran under."""

    scenario_id: UUID
    name: str
    version: int
    blast_radius: tuple[str, ...]
    faults: tuple[ScenarioFault, ...]
    approval_id: UUID  # the decision that made it runnable when the run was queued


class PinnedItem(_Frozen):
    position: int
    step_key: str
    test: PinnedTest | None = None  # exactly one of test and scenario (B-26)
    scenario: PinnedScenario | None = None
    asset: PinnedAsset | None = None
    contract: PinnedContract | None = None
    # B-19: every hop upstream of the target, as declared when the run was queued.
    upstream: tuple[PinnedInput, ...] = ()

    @model_validator(mode="after")
    def _one_kind(self) -> PinnedItem:
        if (self.test is None) == (self.scenario is None):
            raise ValueError("a plan item pins exactly one of a test case and a scenario")
        return self

    @property
    def label(self) -> str:
        """``name vN`` of what the item runs, for messages."""
        what = self.test or self.scenario
        assert what is not None  # noqa: S101 - the validator guarantees one
        return f"{what.name} v{what.version}"


class PinnedEnvironment(_Frozen):
    """Where the run executes (B-22). Its type can only be SANDBOX or STAGING."""

    environment_id: UUID
    name: str
    type: Literal["SANDBOX", "STAGING"]


class Pins(_Frozen):
    format: Literal["rai-run-pins/1"] = PINS_FORMAT
    rai_version: str
    plan: PinnedPlan
    items: list[PinnedItem] = Field(min_length=1)
    # B-22: the plan's environment, else the project's default; absent in B-18 pins.
    environment: PinnedEnvironment | None = None


def step_key(position: int) -> str:
    return f"item-{position}"
