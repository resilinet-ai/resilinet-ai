"""Severity, thresholds and the run verdict (B-12, FR-03.4).

Spec acceptance: a critical failure fails the run.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from rai.validation.contract import load_contract, parse_contract
from rai.validation.engine import read_batch, validate
from rai.validation.history import MemoryKeyStore
from rai.validation.verdict import combine, decide
from sandboxbank.fixtures import build_fixtures

ROOT = Path(__file__).resolve().parents[2]

CONTRACT = parse_contract(
    """
contract: payments
version: 1
columns:
  id:     { type: integer, required: true }
  amount: { type: "decimal(10,2)", min: 0, severity: critical }
  note:   { type: string, pattern: "^[a-z]*$", severity: warn }
  tag:    { type: string, allowed: [a, b], severity: info }
  limit:  { type: integer, max: 10, threshold: "50%" }
"""
)


def frame(**overrides: list[str]) -> pl.DataFrame:
    base = {"id": ["1", "2"], "amount": ["1.00", "2.00"], "note": ["ok", "ok"]}
    base |= {"tag": ["a", "b"], "limit": ["1", "2"]}
    return pl.DataFrame(base | overrides)


def test_clean_batch_passes() -> None:
    verdict = decide(validate(CONTRACT, frame()))
    assert (verdict.outcome, verdict.exit_code, verdict.blocking, verdict.warnings) == (
        "pass",
        0,
        (),
        (),
    )
    assert verdict.summary() == "PASS payments@1"
    assert verdict.counts == {"pass": 11, "fail": 0, "error": 0, "skipped": 0}


def test_a_critical_failure_fails_the_run() -> None:
    verdict = decide(validate(CONTRACT, frame(amount=["-1.00", "2.00"])))
    assert (verdict.outcome, verdict.exit_code) == ("fail", 1)
    assert [r.rule_id for r in verdict.blocking] == ["amount.range"]
    assert verdict.summary() == "FAIL payments@1 - 1 blocking (amount.range)"


def test_warn_and_info_failures_are_warnings_not_failures() -> None:
    verdict = decide(validate(CONTRACT, frame(note=["Bad!", "ok"], tag=["z", "b"])))
    assert verdict.outcome == "pass"
    assert [(r.rule_id, r.severity) for r in verdict.warnings] == [
        ("note.pattern", "warn"),
        ("tag.allowed", "info"),
    ]
    assert verdict.summary() == "PASS payments@1 - 2 warnings (note.pattern, tag.allowed)"


@pytest.mark.parametrize(
    ("fail_on", "outcome", "blocking"),
    [
        ("critical", "pass", []),
        ("warn", "fail", ["note.pattern"]),
        ("info", "fail", ["note.pattern", "tag.allowed"]),
    ],
)
def test_fail_on_raises_the_bar(fail_on: str, outcome: str, blocking: list[str]) -> None:
    report = validate(CONTRACT, frame(note=["Bad!", "ok"], tag=["z", "b"]))
    verdict = decide(report, fail_on=fail_on)  # type: ignore[arg-type]
    assert verdict.outcome == outcome
    assert [r.rule_id for r in verdict.blocking] == blocking


def test_threshold_tolerates_failures_within_it() -> None:
    within = decide(validate(CONTRACT, frame(limit=["99", "2"])))  # 1 of 2 = 50%
    assert within.outcome == "pass"
    beyond = decide(validate(CONTRACT, frame(limit=["99", "99"])))  # 2 of 2
    assert [r.rule_id for r in beyond.blocking] == ["limit.range"]


def test_a_critical_rule_that_cannot_run_fails_the_batch() -> None:
    verdict = decide(validate(CONTRACT, frame().drop("amount")))
    assert verdict.outcome == "fail"
    assert {r.outcome for r in verdict.blocking} == {"fail", "error"}  # schema fails, rules error
    assert "amount.type" in {r.rule_id for r in verdict.blocking}


def test_unknown_fail_on_is_rejected() -> None:
    with pytest.raises(ValueError, match="fail_on must be one of"):
        decide(validate(CONTRACT, frame()), fail_on="fatal")  # type: ignore[arg-type]


def test_verdict_serialises_for_reports() -> None:
    data = decide(validate(CONTRACT, frame(amount=["-1.00", "2.00"]), as_of=None)).to_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["verdict"] == "fail"
    assert data["blocking"][0] == {
        "rule_id": "amount.range",
        "severity": "critical",
        "outcome": "fail",
        "failing_count": 1,
        "row_count": 2,
        "threshold": "0",
        "message": "1 of 2 rows fail",
    }


def test_a_run_fails_if_any_batch_fails() -> None:
    good = decide(validate(CONTRACT, frame()), subject="B1")
    bad = decide(validate(CONTRACT, frame(amount=["x", "1"])), subject="B2")
    run = combine([good, bad, good])
    assert (run.outcome, run.exit_code, [v.subject for v in run.failed]) == ("fail", 1, ["B2"])
    assert run.summary() == "FAIL: 2 of 3 passed; failed: B2"
    assert combine([good]).summary() == "PASS: 1 of 1 passed"
    with pytest.raises(ValueError, match="at least one"):
        combine([])


# ---- acceptance against the sealed SandboxBank fixtures --------------------------------


def test_every_defective_batch_fails_and_every_clean_batch_passes(tmp_path: Path) -> None:
    build_fixtures(
        tmp_path,
        data_seed=51,
        injector_seed=52,
        accounts=200,
        merchants=40,
        rows_per_batch=1_500,
        batches_per_class=2,
        clean=4,
    )
    txn = load_contract(ROOT / "contracts" / "sandboxbank" / "transactions_raw.yaml")
    merchants = load_contract(ROOT / "contracts" / "sandboxbank" / "merchants.yaml")
    refs = {
        "accounts": read_batch(tmp_path / "feed" / "accounts.csv"),
        "merchants": read_batch(tmp_path / "feed" / "merchants.csv"),
    }
    history = MemoryKeyStore()
    truth = [
        json.loads(line)
        for line in (tmp_path / "answer_key" / "ground_truth.jsonl").read_text("utf-8").splitlines()
    ]
    verdicts = []
    for batch in truth:
        report = validate(
            txn,
            read_batch(tmp_path / batch["file"]),
            references=refs,
            as_of=datetime.fromisoformat(batch["onset"]),
            history=history,
            batch_id=batch["batch_id"],
            record=True,
        )
        verdict = decide(report, subject=batch["batch_id"])
        if "dimension_file" in batch:  # D12: the batch is clean, its dimension is not
            assert verdict.passed, batch["batch_id"]
            verdict = decide(
                validate(merchants, read_batch(tmp_path / batch["dimension_file"])),
                subject=f"{batch['batch_id']} merchants",
            )
        expected = "pass" if batch["defect_class"] is None else "fail"
        assert verdict.outcome == expected, (batch["batch_id"], batch["defect_class"])
        verdicts.append(verdict)
    run = combine(verdicts)
    assert run.outcome == "fail"
    assert len(run.failed) == len([b for b in truth if b["defect_class"]])
