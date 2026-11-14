"""Validation reports as deterministic JSON (B-14, FR-06.1).

The JSON report is evidence, so the same inputs must give byte-identical output:
keys are sorted, nothing time-dependent is included (no wall-clock time, no
duration), and the input is identified by its SHA-256 when it is a file. The
human-readable text report is rendered by the CLI from the same data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from rai import __version__
from rai.validation.engine import RuleResult, ValidationReport
from rai.validation.verdict import Verdict

REPORT_FORMAT = "rai-validation-report/1"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rule(result: RuleResult) -> dict[str, Any]:
    data = asdict(result)
    data["columns"] = list(result.columns)
    data["sample_rows"] = list(result.sample_rows)
    return data


def report_dict(
    report: ValidationReport,
    verdict: Verdict,
    *,
    source: dict[str, Any],
    batch_id: str | None,
) -> dict[str, Any]:
    return {
        "format": REPORT_FORMAT,
        "rai_version": __version__,
        "contract": {"ref": report.contract_ref, "sha256": report.contract_sha256},
        "batch": {"id": batch_id, "rows": report.row_count, "source": source},
        "as_of": report.as_of.isoformat() if report.as_of else None,
        "verdict": verdict.to_dict(),
        "rules": [_rule(r) for r in report.results],
        "schema_changes": [asdict(c) for c in report.schema_changes],
        "notes": list(report.notes),
    }


def report_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
