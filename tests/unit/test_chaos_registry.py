"""The sealed S01-S20 chaos registry (B-28; Protocol section 5).

The objectives are the author's, approved on 2026-11-25. These tests fail if the registry
changes after the seal: its canonical SHA-256 is committed in the protocol, and every
objective is checked against the approved table."""

from __future__ import annotations

import re
from pathlib import Path

from rai_bench.chaos import build_plan, check_plan, load_registry

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "benchmarks" / "registry" / "chaos-registry.yaml"
PROTOCOL = ROOT / "docs" / "design" / "rai-protocol.md"
SEALED = "01483388aec94ed63ebd0e953dba6b704172929a23f0ec3a701a7e35b6b4b06c"

# The author's review, 2026-11-25: RTO in minutes as approved; RPO 0 for all twenty.
APPROVED_RTO_MINUTES = {
    "S01": 10, "S02": 10, "S03": 10, "S04": 30, "S05": 15, "S06": 30, "S07": 30,
    "S08": 15, "S09": 45, "S10": 15, "S11": 10, "S12": 45, "S13": 15, "S14": 10,
    "S15": 5, "S16": 30, "S17": 30, "S18": 5, "S19": 15, "S20": 45,
}  # fmt: skip
PIPELINE_PROBES = {"P1": "payments-health", "P2": "etl-ready", "P3": "features-ready"}


def test_the_registry_holds_the_approved_objectives() -> None:
    registry = load_registry(REGISTRY)
    assert [s.id for s in registry.scenarios] == [f"S{n:02d}" for n in range(1, 21)]
    for scenario in registry.scenarios:
        recovery = scenario.definition.recovery
        assert recovery is not None
        assert recovery.rto_seconds == APPROVED_RTO_MINUTES[scenario.id] * 60, scenario.id
        assert recovery.rpo_seconds == 0, scenario.id
        assert recovery.ledger == "payments-ledger"
        assert recovery.window_seconds >= recovery.rto_seconds
        assert scenario.repetitions == 10
        for fault in scenario.definition.faults:  # within SANDBOX-01's 15-minute ceiling
            assert fault.duration_seconds <= 900, scenario.id


def test_p2_and_p3_recovery_is_timed_by_their_freshness_probes() -> None:
    registry = load_registry(REGISTRY)
    for scenario in registry.scenarios:
        recovery = scenario.definition.recovery
        assert recovery is not None
        pipelines = set(re.findall(r"P\d", scenario.title))
        assert set(recovery.probes) == {PIPELINE_PROBES[p] for p in pipelines}, scenario.id


def test_the_registry_matches_its_seal_in_the_protocol(tmp_path: Path) -> None:
    manifest = build_plan(REGISTRY, tmp_path / "plan", seed=2028)
    assert manifest["registry_sha256"] == SEALED, "the sealed registry was edited"
    assert manifest["injections"] == 200
    assert check_plan(tmp_path / "plan") == []
    assert SEALED in PROTOCOL.read_text(encoding="utf-8")
