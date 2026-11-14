"""Guards against tooling drift that would make CI jobs disagree with each other."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

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
