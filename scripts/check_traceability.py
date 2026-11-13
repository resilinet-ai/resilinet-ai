"""Check that a pull-request title names a known requirement ID.

Usage: python scripts/check_traceability.py "feat(FR-03.3): detect duplicate keys"

Titles of type chore, docs, ci, build, test, style or refactor may omit an ID.
Any ID that is present must exist in docs/traceability.csv.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ID_PATTERN = re.compile(r"\b(?:FR-\d{2}\.\d|NFR-[A-Z]+-\d)\b")
EXEMPT_TYPES = ("chore", "docs", "ci", "build", "test", "style", "refactor")
CSV_PATH = Path(__file__).resolve().parent.parent / "docs" / "traceability.csv"


def known_ids(csv_path: Path = CSV_PATH) -> set[str]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return {row["id"].strip() for row in csv.DictReader(handle)}


def check_title(title: str, ids: set[str]) -> tuple[bool, str]:
    found = ID_PATTERN.findall(title)
    unknown = sorted(set(found) - ids)
    if unknown:
        return False, f"Unknown requirement ID(s): {', '.join(unknown)}"
    if found:
        return True, f"Traced to {', '.join(sorted(set(found)))}"
    commit_type = title.split("(", 1)[0].split(":", 1)[0].strip().lower()
    if commit_type in EXEMPT_TYPES:
        return True, f"'{commit_type}' change: no requirement ID needed"
    return False, "Title must name a requirement ID, e.g. 'feat(FR-03.3): ...'"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    ok, message = check_title(argv[1], known_ids())
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
