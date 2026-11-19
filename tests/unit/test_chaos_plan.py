"""Sealed fault plans for the chaos benchmark (B-28; Protocol sections 5 and 8; threat T-06).

The scenarios here are test fixtures (S98, S99), not the registered S01-S20.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rai_bench.chaos import build_plan, check_plan, load_registry, load_sealed_plan
from rai_bench.cli import app
from rai_bench.runner import HarnessError

runner = CliRunner()

REGISTRY = """\
format: rai-bench-chaos-registry/1
scenarios:
  - id: S98
    title: Test fixture, latency on the ledger proxy
    repetitions: 2
    definition:
      blast_radius: [ledger-proxy, payments-api]
      faults:
        - {type: latency, target: ledger-proxy, duration_seconds: 30, latency_ms: 500}
      recovery:
        probes: [payments-api]
        ledger: payments-api
        rto_seconds: 60
        rpo_seconds: 0
        window_seconds: 120
  - id: S99
    title: Test fixture, the ETL stopped then restarted
    repetitions: 3
    definition:
      blast_radius: [etl-job, payments-api]
      faults:
        - {type: container_stop, target: etl-job, duration_seconds: 20}
        - {type: container_restart, target: etl-job, duration_seconds: 10}
      recovery:
        probes: [payments-api]
        ledger: payments-api
        rto_seconds: 90
        rpo_seconds: 5
"""


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    return path


@pytest.fixture
def plan(registry: Path, tmp_path: Path) -> Path:
    out = tmp_path / "plan"
    build_plan(registry, out, seed=7)
    return out


def _lines(plan: Path) -> list[dict[str, object]]:
    text = (plan / "answer_key" / "fault_log.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_same_inputs_give_identical_bytes_and_seeds_change_the_schedule(
    registry: Path, plan: Path, tmp_path: Path
) -> None:
    again = tmp_path / "again"
    build_plan(registry, again, seed=7)
    for name in ("registry.json", "answer_key/fault_log.jsonl", "answer_key/SEAL", "manifest.json"):
        assert (plan / name).read_bytes() == (again / name).read_bytes()
    other = tmp_path / "other"
    build_plan(registry, other, seed=8)
    assert _lines(other) != _lines(plan)


def test_the_plan_holds_every_repetition_spaced_by_its_faults_and_recovery(plan: Path) -> None:
    lines = _lines(plan)
    assert [line["injection_id"] for line in lines] == [f"I{n:04d}" for n in range(1, 6)]
    assert sorted(str(line["scenario"]) for line in lines) == ["S98"] * 2 + ["S99"] * 3
    registry = json.loads((plan / "registry.json").read_text(encoding="utf-8"))
    hashes = {s["id"]: s["sha256"] for s in registry["scenarios"]}
    busy = {"S98": 30 + 120, "S99": 20 + 10 + 300}  # fault durations + recovery window
    previous_end = 0
    for line in lines:
        assert line["scenario_sha256"] == hashes[line["scenario"]]
        start = int(str(line["start_offset_seconds"]))
        assert 60 <= start - previous_end <= 600  # the default seeded gap
        previous_end = start + busy[str(line["scenario"])]
    s99 = next(line for line in lines if line["scenario"] == "S99")
    assert s99["location"] == ["etl-job"]
    assert (s99["rto_seconds"], s99["rpo_seconds"]) == (90, 5)
    assert [f["type"] for f in s99["faults"]] == ["container_stop", "container_restart"]

    manifest = json.loads((plan / "manifest.json").read_text(encoding="utf-8"))
    seal = (plan / "answer_key" / "SEAL").read_text(encoding="utf-8").strip()
    assert manifest["fault_log_sha256"] == seal
    assert (manifest["seed"], manifest["seed_purpose"], manifest["injections"]) == (
        7,
        "development",
        5,
    )
    assert set(manifest["files"]) == {
        "registry.json",
        "answer_key/fault_log.jsonl",
        "answer_key/SEAL",
    }
    assert check_plan(plan) == []
    assert check_plan(plan, seal.upper()) == []


def test_a_changed_fault_log_breaks_the_seal(plan: Path) -> None:
    fault_log = plan / "answer_key" / "fault_log.jsonl"
    lines = _lines(plan)
    lines[0]["start_offset_seconds"] = 1
    fault_log.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    assert "the fault log does not match its SEAL" in check_plan(plan)


def test_resealing_locally_is_caught_by_the_public_hash(plan: Path) -> None:
    """Rewriting the log, its SEAL and the manifest together still fails --expect."""
    public = (plan / "answer_key" / "SEAL").read_text(encoding="utf-8").strip()
    fault_log = plan / "answer_key" / "fault_log.jsonl"
    fault_log.write_text(fault_log.read_text(encoding="utf-8").replace("I0001", "I0009"))
    from sandboxbank.dataset import sha256_file

    new = sha256_file(fault_log)
    (plan / "answer_key" / "SEAL").write_text(new + "\n", encoding="utf-8")
    manifest_file = plan / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["fault_log_sha256"] = new
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    assert check_plan(plan) == []
    assert check_plan(plan, public) == ["the fault log does not match the publicly committed hash"]


def test_a_loosened_objective_no_longer_matches_the_sealed_log(plan: Path) -> None:
    """Changing an RTO in registry.json (and its hash there) after sealing is caught."""
    registry_file = plan / "registry.json"
    registry = json.loads(registry_file.read_text(encoding="utf-8"))
    registry["scenarios"][1]["definition"]["recovery"]["rto_seconds"] = 900
    registry_file.write_text(json.dumps(registry), encoding="utf-8")
    problems = check_plan(plan)
    assert "registry.json does not match the manifest" in problems
    assert sum("'S99' does not match its registered definition" in p for p in problems) == 3


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        (lambda p: (p / "answer_key" / "SEAL").unlink(), "missing answer_key/SEAL"),
        (lambda p: (p / "manifest.json").write_text("[]"), "manifest.json is not a JSON object"),
        (lambda p: (p / "registry.json").write_text("{}"), "registry.json is not a valid registry"),
    ],
)
def test_damaged_plans_are_reported(plan: Path, damage: object, problem: str) -> None:
    damage(plan)
    assert problem in check_plan(plan)


def test_load_sealed_plan_refuses_broken_seals_and_unpermitted_evaluation(
    registry: Path, plan: Path, tmp_path: Path
) -> None:
    assert len(load_sealed_plan(plan)) == 5
    evaluation = tmp_path / "evaluation"
    build_plan(registry, evaluation, seed=150_000)
    with pytest.raises(HarnessError, match="evaluation plan"):
        load_sealed_plan(evaluation)
    assert len(load_sealed_plan(evaluation, allow_evaluation=True)) == 5
    (plan / "answer_key" / "SEAL").write_text("0" * 64, encoding="utf-8")
    with pytest.raises(HarnessError, match="does not match its SEAL"):
        load_sealed_plan(plan)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (("rpo_seconds: 5\n", ""), "needs recovery probes and both rto_seconds and rpo_seconds"),
        (("id: S99", "id: S98"), "lists a scenario id twice"),
        (("id: S99", "id: X99"), "scenarios.1.id"),
        (("repetitions: 3", "repetitions: 0"), "repetitions"),
        (("format: rai-bench-chaos-registry/1", "format: other"), "format"),
        (("scenarios:", "scenarios: ["), "not valid YAML"),
    ],
)
def test_invalid_registries_are_refused(
    tmp_path: Path, edit: tuple[str, str], message: str
) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(REGISTRY.replace(*edit), encoding="utf-8")
    with pytest.raises(HarnessError, match=message):
        load_registry(path)


def test_plans_need_protocol_seeds_sane_gaps_and_an_empty_folder(
    registry: Path, plan: Path, tmp_path: Path
) -> None:
    with pytest.raises(HarnessError, match="outside the protocol ranges"):
        build_plan(registry, tmp_path / "a", seed=200_000)
    with pytest.raises(HarnessError, match="min_gap_seconds"):
        build_plan(registry, tmp_path / "b", seed=1, min_gap_seconds=10, max_gap_seconds=5)
    with pytest.raises(HarnessError, match="never overwritten"):
        build_plan(registry, plan, seed=7)
    with pytest.raises(HarnessError, match="could not read"):
        build_plan(tmp_path / "missing.yaml", tmp_path / "c", seed=1)


def test_cli_plans_checks_and_reports(registry: Path, tmp_path: Path) -> None:
    out = tmp_path / "cli-plan"
    result = runner.invoke(app, ["chaos-plan", str(registry), "--out", str(out), "--seed", "3"])
    assert result.exit_code == 0, result.output
    seal = (out / "answer_key" / "SEAL").read_text(encoding="utf-8").strip()
    assert f"Fault log SEAL: {seal}" in result.output
    assert "5 injections of 2 scenarios" in result.output

    result = runner.invoke(app, ["chaos-seal", str(out), "--expect", seal])
    assert result.exit_code == 0, result.output
    (out / "answer_key" / "fault_log.jsonl").write_text("{}\n", encoding="utf-8")
    result = runner.invoke(app, ["chaos-seal", str(out)])
    assert result.exit_code == 1
    assert "does not match its SEAL" in result.output

    result = runner.invoke(app, ["chaos-plan", str(registry), "--out", str(out), "--seed", "3"])
    assert result.exit_code == 2
    assert "never overwritten" in result.output
