"""NFR-SAF-2's evidence stays real: every test docs/chaos-agent.md names for the four
safety limits (maximum duration, blast radius, rollback, the kill switch) exists."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "chaos-agent.md"
LIMITS = ("Maximum duration", "Blast radius", "Rollback", "Kill switch without the API")


def section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("## Safety limits (NFR-SAF-2)")
    return text[start : text.index("\n## ", start + 1)]


def test_every_limit_has_a_row_with_evidence() -> None:
    rows = {
        m.group(1): m.group(2)
        for m in re.finditer(r"^\| \*\*(.+?)\*\* \|.*\| (.+) \|$", section(), re.MULTILINE)
    }
    assert set(rows) == set(LIMITS)
    for limit in LIMITS:
        assert re.search(r"`[^`]+::\w+`", rows[limit]), f"{limit} names no test"


def test_every_named_test_exists() -> None:
    named = re.findall(r"`([\w./]+\.py)::(\w+)`", section())
    assert len(named) >= 16
    for path, name in named:
        source = (ROOT / path).read_text(encoding="utf-8")
        assert re.search(rf"^def {name}\(", source, re.MULTILINE), f"{path}::{name} is gone"
