"""Guards against tooling drift that would make CI jobs disagree with each other."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_pre_commit_ruff_matches_the_locked_ruff() -> None:
    # CI formats with the locked ruff (uv.lock) and pre-commit with its own pin; if the
    # versions differ, their formatting styles can disagree and one job always fails.
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = next(p["version"] for p in lock["package"] if p["name"] == "ruff")
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    pinned = re.search(r"ruff-pre-commit\s+rev:\s*v([0-9.]+)", config)
    assert pinned is not None
    assert pinned.group(1) == locked, f"set ruff-pre-commit rev to v{locked}"


def test_uv_never_resolves_a_release_younger_than_14_days() -> None:
    # Decision DEP-MIN-AGE (threat model T-08): the lock itself obeys the two-week rule.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["uv"]["exclude-newer"] == "14 days"
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert lock["options"]["exclude-newer-span"] == "P14D", "run uv lock"


def test_every_dependabot_update_waits_at_least_14_days() -> None:
    # Decision DEP-COOLDOWN (threat model T-08): no release younger than two weeks.
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    updates = config["updates"]
    assert updates
    for update in updates:
        days = update.get("cooldown", {}).get("default-days", 0)
        assert days >= 14, f"{update['package-ecosystem']}: set cooldown default-days: 14"


def test_the_chaos_agent_image_installs_exactly_the_locked_versions() -> None:
    # B-24: the agent image installs requirements.txt with --require-hashes; it must pin
    # the versions uv.lock resolved (regenerate with the uv export command in its header).
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {p["name"]: p["version"] for p in lock["package"]}
    agent = next(p for p in lock["package"] if p["name"] == "rai-chaos-agent")
    text = (ROOT / "chaos_agent" / "requirements.txt").read_text(encoding="utf-8")
    pinned = dict(re.findall(r"^([a-z0-9_.-]+)==([^\s;]+)", text, flags=re.MULTILINE))
    assert {d["name"] for d in agent["dependencies"]} <= set(pinned)
    for name, version in pinned.items():
        assert locked.get(name) == version, f"{name}: run the uv export in requirements.txt"
    assert "--hash=sha256:" in text
