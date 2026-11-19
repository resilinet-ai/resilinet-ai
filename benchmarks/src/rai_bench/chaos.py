"""Sealed fault plans for the chaos benchmark (B-28; Protocol sections 5, 8 and 10; threat T-06).

The author pre-registers the scenarios (S01 to S20) in a registry file: for each,
its faults, blast radius, recovery probes, RTO and RPO objectives, and how many
times it runs. ``build_plan`` turns the registry and a seed into the **planned
fault log**: which scenario is injected in which order, and the earliest each may
start. That log is the chaos benchmark's answer key. Its SHA-256 (the SEAL) is
committed publicly before any arm runs, so neither the schedule nor the
objectives can be changed after the results are known.

Layout::

    <out>/registry.json               the registered scenarios, canonical, with their hashes
    <out>/answer_key/fault_log.jsonl  the planned fault log: one line per injection, in order
    <out>/answer_key/SEAL             SHA-256 of fault_log.jsonl
    <out>/manifest.json               seed, gaps, registry hash, seal, hash of every file

Arms may see ``registry.json`` (it is registered publicly); they never see
``answer_key/``. Seeds follow the protocol ranges, as for the defect fixtures:
development 0-99,999, evaluation 100,000-199,999.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from rai.core.scenarios import ScenarioDefinition
from rai_bench import __version__
from rai_bench.runner import HarnessError
from sandboxbank.dataset import sha256_file
from sandboxbank.generator import seed_purpose, stream

REGISTRY_FORMAT = "rai-bench-chaos-registry/1"
FAULT_LOG_FORMAT = "rai-bench-fault/1"
MAX_SCENARIOS = 100
DEFAULT_MIN_GAP_SECONDS = 60
DEFAULT_MAX_GAP_SECONDS = 600


class RegisteredScenario(BaseModel):
    """One pre-registered scenario. The definition is the one the RAI API accepts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^S\d{2}$")
    title: str = Field(min_length=1, max_length=200)
    repetitions: int = Field(ge=1, le=100)
    definition: ScenarioDefinition

    @model_validator(mode="after")
    def _objectives(self) -> RegisteredScenario:
        # Protocol section 5: every scenario is registered with recovery probes and
        # RTO/RPO objectives, so an objective cannot be chosen after the run.
        recovery = self.definition.recovery
        if recovery is None or recovery.rto_seconds is None or recovery.rpo_seconds is None:
            raise ValueError(
                f"{self.id}: a registered scenario needs recovery probes and both "
                "rto_seconds and rpo_seconds"
            )
        return self

    def sha256(self) -> str:
        return hashlib.sha256(_dump(self.model_dump(mode="json")).encode()).hexdigest()


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal["rai-bench-chaos-registry/1"]
    scenarios: tuple[RegisteredScenario, ...] = Field(min_length=1, max_length=MAX_SCENARIOS)

    @model_validator(mode="after")
    def _unique(self) -> Registry:
        ids = [s.id for s in self.scenarios]
        if len(set(ids)) != len(ids):
            raise ValueError("the registry lists a scenario id twice")
        return self


def _dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_registry(path: Path) -> Registry:
    """Read a registry from YAML or JSON (JSON is valid YAML)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Registry.model_validate(raw)
    except OSError as error:
        raise HarnessError(f"could not read the registry {path}: {error.strerror}") from None
    except yaml.YAMLError as error:
        raise HarnessError(f"{path} is not valid YAML: {error}") from None
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'registry'}: {e['msg']}"
            for e in error.errors()
        )
        raise HarnessError(f"{path} is not a valid registry: {problems}") from None


def build_plan(
    registry_file: Path,
    out_dir: Path,
    *,
    seed: int,
    min_gap_seconds: int = DEFAULT_MIN_GAP_SECONDS,
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
) -> dict[str, Any]:
    """Write a sealed fault plan and return its manifest. Same inputs give identical bytes.

    Every repetition of every scenario becomes one injection, shuffled by the seed.
    An injection may start no earlier than ``start_offset_seconds`` after the run
    starts: the previous injection's start, plus all its fault durations and its
    recovery window, plus a seeded gap, so arms cannot time the faults.
    """
    try:
        purpose = seed_purpose(seed)
    except ValueError as error:
        raise HarnessError(str(error)) from None
    if not 0 <= min_gap_seconds <= max_gap_seconds:
        raise HarnessError("the gaps need 0 <= min_gap_seconds <= max_gap_seconds")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise HarnessError(f"{out_dir} is not empty; sealed plans are never overwritten")
    registry = load_registry(registry_file)

    slots = [(s, rep) for s in registry.scenarios for rep in range(1, s.repetitions + 1)]
    rng = stream(seed, "chaos/plan")
    rng.shuffle(slots)
    hashes = {s.id: s.sha256() for s in registry.scenarios}

    lines: list[str] = []
    offset = 0
    for n, (scenario, repetition) in enumerate(slots, start=1):
        offset += rng.randint(min_gap_seconds, max_gap_seconds)
        definition = scenario.definition
        recovery = definition.recovery
        assert recovery is not None  # noqa: S101 - RegisteredScenario guarantees it
        lines.append(
            _dump(
                {
                    "format": FAULT_LOG_FORMAT,
                    "injection_id": f"I{n:04d}",
                    "scenario": scenario.id,
                    "scenario_sha256": hashes[scenario.id],
                    "repetition": repetition,
                    "start_offset_seconds": offset,
                    "faults": [f.model_dump(mode="json") for f in definition.faults],
                    "location": sorted({f.target for f in definition.faults}),
                    "blast_radius": sorted(definition.blast_radius),
                    "rto_seconds": recovery.rto_seconds,
                    "rpo_seconds": recovery.rpo_seconds,
                }
            )
        )
        offset += sum(f.duration_seconds for f in definition.faults) + recovery.window_seconds

    out_dir.mkdir(parents=True, exist_ok=True)
    registry_json = {
        "format": REGISTRY_FORMAT,
        "scenarios": [
            {**s.model_dump(mode="json"), "sha256": hashes[s.id]} for s in registry.scenarios
        ],
    }
    _write(out_dir / "registry.json", json.dumps(registry_json, indent=2, sort_keys=True) + "\n")
    fault_log = out_dir / "answer_key" / "fault_log.jsonl"
    fault_log.parent.mkdir(parents=True, exist_ok=True)
    _write(fault_log, "\n".join(lines) + "\n")
    seal = sha256_file(fault_log)
    _write(out_dir / "answer_key" / "SEAL", seal + "\n")

    files = sorted(
        p.relative_to(out_dir).as_posix()
        for p in out_dir.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    )
    manifest = {
        "generator": "rai-bench",
        "generator_version": __version__,
        "kind": "chaos fault plan",
        "synthetic": True,
        "seed": seed,
        "seed_purpose": purpose,
        "config": {"min_gap_seconds": min_gap_seconds, "max_gap_seconds": max_gap_seconds},
        "scenarios": len(registry.scenarios),
        "injections": len(lines),
        "registry_sha256": sha256_file(out_dir / "registry.json"),
        "fault_log_sha256": seal,
        "files": {path: sha256_file(out_dir / path) for path in files},
    }
    _write(out_dir / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def check_plan(out_dir: Path, expected: str | None = None) -> list[str]:
    """What is wrong with a sealed plan; empty if the fault log still matches its SEAL
    (and ``expected``, the publicly committed hash), the manifest, and the registry."""
    fault_log = out_dir / "answer_key" / "fault_log.jsonl"
    seal_file = out_dir / "answer_key" / "SEAL"
    registry_file = out_dir / "registry.json"
    manifest_file = out_dir / "manifest.json"
    required = (fault_log, seal_file, registry_file, manifest_file)
    missing = [p.relative_to(out_dir).as_posix() for p in required if not p.is_file()]
    if missing:
        return [f"missing {name}" for name in missing]

    problems: list[str] = []
    actual = sha256_file(fault_log)
    if actual != seal_file.read_text(encoding="utf-8").strip():
        problems.append("the fault log does not match its SEAL")
    if expected is not None and actual != expected.strip().lower():
        problems.append("the fault log does not match the publicly committed hash")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except ValueError:
        manifest = None
    if not isinstance(manifest, dict):
        return [*problems, "manifest.json is not a JSON object"]
    if manifest.get("fault_log_sha256") != actual:
        problems.append("the fault log does not match the manifest")
    if manifest.get("registry_sha256") != sha256_file(registry_file):
        problems.append("registry.json does not match the manifest")

    try:
        raw = json.loads(registry_file.read_text(encoding="utf-8"))
        # registry.json adds each scenario's hash; the hash is recomputed, not trusted.
        scenarios = [{k: v for k, v in s.items() if k != "sha256"} for s in raw["scenarios"]]
        registry = Registry.model_validate({**raw, "scenarios": scenarios})
    except (ValueError, KeyError, TypeError, AttributeError):
        return [*problems, "registry.json is not a valid registry"]
    hashes = {s.id: s.sha256() for s in registry.scenarios}
    for number, line in enumerate(fault_log.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            problems.append(f"fault log line {number} is not a JSON object")
            continue
        if hashes.get(str(entry.get("scenario"))) != entry.get("scenario_sha256"):
            problems.append(
                f"fault log line {number}: scenario {entry.get('scenario')!r} does not match "
                "its registered definition"
            )
    return problems


def load_sealed_plan(
    out_dir: Path, expected: str | None = None, *, allow_evaluation: bool = False
) -> list[dict[str, Any]]:
    """The planned injections, for whatever runs or scores the chaos benchmark. Refuses a
    plan whose seal is broken, and an evaluation plan unless explicitly allowed."""
    problems = check_plan(out_dir, expected)
    if problems:
        raise HarnessError(f"{out_dir}: {'; '.join(problems)}; not using this plan")
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("seed_purpose") == "evaluation" and not allow_evaluation:
        raise HarnessError(
            f"{out_dir} is an evaluation plan; pass allow_evaluation for the registered "
            "benchmark only"
        )
    text = (out_dir / "answer_key" / "fault_log.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]
