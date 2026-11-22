"""Guards against tooling drift that would make CI jobs disagree with each other."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
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


@pytest.mark.parametrize(
    ("folder", "package"),
    [("chaos_agent", "rai-chaos-agent"), ("sandboxbank", "rai-sandboxbank")],
)
def test_the_workspace_images_install_exactly_the_locked_versions(
    folder: str, package: str
) -> None:
    # B-24, B-40: these images install requirements.txt with --require-hashes; it must pin
    # the versions uv.lock resolved (regenerate with the uv export command in its header).
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {p["name"]: p["version"] for p in lock["package"]}
    member = next(p for p in lock["package"] if p["name"] == package)
    text = (ROOT / folder / "requirements.txt").read_text(encoding="utf-8")
    pinned = dict(re.findall(r"^([a-z0-9_.-]+)==([^\s;]+)", text, flags=re.MULTILINE))
    assert {d["name"] for d in member["dependencies"]} <= set(pinned)
    for name, version in pinned.items():
        assert locked.get(name) == version, f"{name}: run the uv export in requirements.txt"
    assert "--hash=sha256:" in text


def _runtime_closure(lock: dict[str, object], root: str) -> set[str]:
    """Every distribution ``root`` installs without dev groups, by its normalised name."""
    packages = {p["name"]: p for p in lock["package"]}  # type: ignore[attr-defined]
    seen: set[str] = set()
    todo = [root]
    while todo:
        name = todo.pop()
        if name in seen or name not in packages:
            continue
        seen.add(name)
        todo.extend(d["name"] for d in packages[name].get("dependencies", []))
    return seen


def test_rai_imports_only_what_it_installs() -> None:
    # The API image installs resilinet-ai without dev groups. A module imported from a
    # dev-only package (httpx, before FR-01.3) passes every test and breaks the image.
    import ast
    import sys
    from importlib.metadata import packages_distributions

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    installed = _runtime_closure(lock, "resilinet-ai")
    owners = packages_distributions()
    missing = set()
    for path in (ROOT / "src" / "rai").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top == "rai" or top in sys.stdlib_module_names:
                    continue
                dists = {re.sub(r"[-_.]+", "-", d).lower() for d in owners.get(top, [top])}
                if not dists & installed:
                    missing.add(f"{top} ({path.relative_to(ROOT).as_posix()})")
    assert not missing, f"imported but not a runtime dependency: {sorted(missing)}"
