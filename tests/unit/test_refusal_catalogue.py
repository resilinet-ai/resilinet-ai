"""The refusal catalogue stays true (B-27: NFR-SAF-1; threat E-02's refusal suite).

docs/refusals.md lists every way a fault is refused and the test that proves it.
This fails if a cited test or check no longer exists, or a cited threat is not in
the threat model, so the catalogue cannot quietly drift from the code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "docs" / "refusals.md"
PROOF = re.compile(r"`((?:tests|scripts)/[\w/]+\.py)::(\w+)`")
THREAT = re.compile(r"\b([STRIDE]-\d{2})\b")


def rows() -> list[str]:
    lines = CATALOGUE.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.startswith("| ") and not line.startswith("| ---")
            and not line.startswith(("| Refusal ", "| Control "))]  # fmt: skip


def functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_every_row_cites_a_threat_and_a_proof() -> None:
    assert len(rows()) >= 20
    for row in rows():
        assert THREAT.search(row), row
        assert PROOF.search(row), row


def test_every_cited_test_exists() -> None:
    missing = []
    for path, name in PROOF.findall(CATALOGUE.read_text(encoding="utf-8")):
        file = ROOT / path
        if not file.is_file() or name not in functions(file):
            missing.append(f"{path}::{name}")
    assert missing == []


def test_every_cited_threat_is_in_the_threat_model() -> None:
    model = (ROOT / "docs" / "threat-model.md").read_text(encoding="utf-8")
    known = set(re.findall(r"^\| ([STRIDE]-\d{2}) \|", model, flags=re.MULTILINE))
    cited = set(THREAT.findall(CATALOGUE.read_text(encoding="utf-8")))
    assert cited - known == set()
