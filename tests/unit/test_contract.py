"""Data-contract parser, validation and rule expansion (B-08: FR-03.1, FR-03.4)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rai.cli.main import app
from rai.validation.contract import (
    ContractError,
    Threshold,
    contract_json_schema,
    load_contract,
    parse_contract,
    parse_duration,
    parse_threshold,
    parse_type,
)

ROOT = Path(__file__).resolve().parents[2]
SANDBOXBANK = ROOT / "contracts" / "sandboxbank"

MINIMAL = """
contract: payments
version: 2
columns:
  id: { type: uuid, required: true }
"""


def problems(text: str) -> list[str]:
    with pytest.raises(ContractError) as info:
        parse_contract(text)
    return info.value.problems


# ---- the shipped SandboxBank contracts ----------------------------------------


def test_sandboxbank_contracts_parse_and_expand() -> None:
    counts = {path.stem: len(load_contract(path).rules()) for path in SANDBOXBANK.glob("*.yaml")}
    assert counts == {"transactions_raw": 36, "accounts": 22, "merchants": 15}


def test_every_defect_class_has_a_rule_in_the_sandboxbank_contracts() -> None:
    txn = {rule.id for rule in load_contract(SANDBOXBANK / "transactions_raw.yaml").rules()}
    merchants = {rule.id for rule in load_contract(SANDBOXBANK / "merchants.yaml").rules()}
    catches = {
        "D01": "txn_id.unique" in txn,
        "D02": "unique_across_batches:txn_id" in txn,
        "D03": {"txn_id.required", "amount.required", "created_at.required"} <= txn,
        "D04": "amount.type" in txn,
        "D05": {"amount.range", "amount.max_lookup"} <= txn,
        "D06": "account_id.references" in txn,
        "D07": "schema" in txn,
        "D08": "schema" in txn,
        "D09": "schema" in txn and "amount.type" in txn,
        "D10": "freshness" in txn,
        "D11": "unique_across_batches:txn_id" in txn,
        "D12": "merchant_id.unique" in merchants,
    }
    assert all(catches.values()), [d for d, ok in catches.items() if not ok]


def test_rule_details_follow_the_contract() -> None:
    rules = {r.id: r for r in load_contract(SANDBOXBANK / "transactions_raw.yaml").rules()}
    assert rules["amount.range"].params == {"min": 0.01, "max": 25000}
    assert rules["amount.max_lookup"].params == {
        "table": "accounts",
        "column": "txn_limit",
        "key": "account_id",
    }
    assert rules["account_id.references"].params == {"table": "accounts", "column": "account_id"}
    assert rules["freshness"].params == {"max_lag_seconds": 3600}
    assert rules["is_fraud.type"].severity == "warn"
    assert rules["amount.type"].severity == "critical"
    assert rules["schema"].params["policy"]["removed_or_renamed_column"] == "breaking"
    assert "merchant_id.required" not in rules


# ---- identity and canonical form ----------------------------------------------------


def test_equivalent_yaml_gives_the_same_sha256() -> None:
    flow = parse_contract(MINIMAL)
    block = parse_contract(
        "version: 2\ncolumns:\n  id:\n    required: true\n    type: UUID\ncontract: payments\n"
    )
    assert flow.spec_sha256 == block.spec_sha256
    assert flow.ref == "payments@2"
    assert json.loads(flow.canonical_json())["columns"]["id"]["type"] == "uuid"


def test_any_change_changes_the_sha256() -> None:
    base = parse_contract(MINIMAL)
    assert (
        parse_contract(MINIMAL.replace("version: 2", "version: 3")).spec_sha256 != base.spec_sha256
    )
    assert parse_contract(MINIMAL.replace("required: true", "required: false")).spec_sha256 != (
        base.spec_sha256
    )


def test_defaults_and_composite_primary_key() -> None:
    contract = parse_contract(
        """
contract: positions
version: 1
primary_key: [account_id, as_of]
threshold_default: "0.5%"
columns:
  account_id: { type: string }
  as_of: { type: date }
  qty: { type: integer, min: 0, threshold: 3, severity: info }
"""
    )
    rules = {r.id: r for r in contract.rules()}
    assert rules["primary_key"].columns == ("account_id", "as_of")
    assert "account_id.unique" not in rules  # composite key: no single-column uniqueness
    assert {"account_id.required", "as_of.required"} <= set(rules)
    assert str(rules["account_id.type"].threshold) == "0.5%"
    assert rules["qty.range"].threshold == Threshold(max_count=3)
    assert rules["qty.range"].severity == "info"
    assert rules["schema"].severity == "critical"


# ---- validation messages ------------------------------------------------------------


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        ("contract: X\nversion: 1\ncolumns: {a: {type: string}}", "contract: String should match"),
        (
            "contract: x\nversion: 0\ncolumns: {a: {type: string}}",
            "version: Input should be greater",
        ),
        ("contract: x\nversion: 1\ncolumns: {}", "columns: Dictionary should have at least 1"),
        (
            "contract: x\nversion: 1\ncolumns: {a: {type: text}}",
            "columns.a.type: unknown type 'text'",
        ),
        (
            "contract: x\nversion: 1\ncolumns: {a: {type: string, colour: red}}",
            "colour: Extra inputs",
        ),
        (
            "contract: x\nversion: 1\ncolumns: {a: {type: string, min: 1}}",
            "min/max need an ordered type",
        ),
        (
            "contract: x\nversion: 1\ncolumns: {a: {type: integer, pattern: x}}",
            "pattern needs a string",
        ),
        (
            "contract: x\nversion: 1\ncolumns: {a: {type: string, pattern: '(' }}",
            "not a valid regular",
        ),
        (
            "contract: x\nversion: 1\ncolumns: {a: {type: integer, min: 5, max: 1}}",
            "min (5) is greater",
        ),
        (
            "contract: x\nversion: 1\ncolumns: {a: {type: integer, allowed: [x]}}",
            "allowed value 'x'",
        ),
        ("contract: x\nversion: 1\ncolumns: {a: {type: integer, min: true}}", "not a boolean"),
        ("contract: x\nversion: 1\ncolumns: {a: {type: date, min: 5}}", "ISO 8601 strings"),
        (
            "contract: x\nversion: 1\ncolumns: {a: {type: string, references: accounts}}",
            "references",
        ),
        ("contract: x\nversion: 1\ncolumns: {a: {type: string, threshold: lots}}", "row count or"),
        (
            "contract: x\nversion: 1\nprimary_key: [b]\ncolumns: {a: {type: string}}",
            "'b' is not declared",
        ),
        ("contract: x\nversion: 1\nprimary_key: [a, a]\ncolumns: {a: {type: string}}", "twice"),
        (
            "contract: x\nversion: 1\nfreshness: {column: a, max_lag: 1h}\ncolumns: {a: {type: string}}",
            "timestamp",
        ),
        (
            "contract: x\nversion: 1\nfreshness: {column: b, max_lag: 1h}\ncolumns: {a: {type: date}}",
            "'b' is not",
        ),
        (
            "contract: x\nversion: 1\nfreshness: {column: a, max_lag: soon}\ncolumns: {a: {type: date}}",
            "30s, 15m",
        ),
        (
            "contract: x\nversion: 1\ncolumns: {a: {type: string, max_lookup: {ref: t.c, key: a}}}",
            "max_lookup needs a numeric",
        ),
        (
            "contract: x\nversion: 1\ncolumns: {a: {type: integer, max_lookup: {ref: t.c, key: z}}}",
            "max_lookup.key: column 'z'",
        ),
        (
            "contract: x\nversion: 1\ncolumns:\n  a: { type: decimal(14,2) }",
            "looks cut at the comma",
        ),
        (
            "contract: x\nversion: 1\ncolumns:\n  a: {type: string}\n  a: {type: integer}",
            "duplicate key 'a'",
        ),
        ("- just\n- a list", "must be a YAML mapping"),
        ("contract: [unclosed", "not valid YAML"),
    ],
)
def test_invalid_contracts_explain_themselves(yaml_text: str, expected: str) -> None:
    assert any(expected in problem for problem in problems(yaml_text)), problems(yaml_text)


def test_all_problems_are_reported_at_once() -> None:
    found = problems("contract: x\nversion: 0\ncolumns: {a: {type: nope}, b: {type: string, x: 1}}")
    assert len(found) == 3


# ---- small parsers -------------------------------------------------------------------


def test_type_parsing() -> None:
    assert str(parse_type("DECIMAL( 18 , 2 )")) == "decimal(18,2)"
    assert str(parse_type("decimal")) == "decimal(38,2)"
    with pytest.raises(ValueError, match="precision"):
        parse_type("decimal(5,6)")


def test_durations_and_thresholds() -> None:
    assert parse_duration("90s").total_seconds() == 90
    assert parse_duration("2d").total_seconds() == 172_800
    with pytest.raises(ValueError, match="duration"):
        parse_duration("0m")
    percent = parse_threshold("0.5%")
    assert percent.max_fraction == Decimal("0.005")
    assert percent.allows(5, 1_000)
    assert not percent.allows(6, 1_000)
    assert percent.allows(0, 0)
    count = parse_threshold(2)
    assert count.allows(2, 10)
    assert not count.allows(3, 10)
    assert Threshold().allows(0, 5)
    assert not Threshold().allows(1, 5)
    assert (str(percent), str(count)) == ("0.5%", "2")
    for bad in (-1, True, "150%", "x"):
        with pytest.raises(ValueError, match="threshold"):
            parse_threshold(bad)  # type: ignore[arg-type]


# ---- JSON Schema and CLI ------------------------------------------------------------------


def test_committed_json_schema_is_current() -> None:
    committed = json.loads((ROOT / "contracts" / "contract.schema.json").read_text("utf-8"))
    assert committed == contract_json_schema(), (
        "run: rai contract schema > contracts/contract.schema.json"
    )


def test_cli_contract_check(tmp_path: Path) -> None:
    runner = CliRunner()
    ok = runner.invoke(app, ["contract", "check", str(SANDBOXBANK / "transactions_raw.yaml")])
    assert ok.exit_code == 0
    assert "transactions_raw@1" in ok.output
    assert "36 rules" in ok.output

    bad = tmp_path / "bad.yaml"
    bad.write_text("contract: x\nversion: 1\ncolumns: {a: {type: nope}}\n", encoding="utf-8")
    failed = runner.invoke(
        app, ["contract", "check", str(bad), str(SANDBOXBANK / "merchants.yaml")]
    )
    assert failed.exit_code == 1
    assert "INVALID" in failed.output
    assert "merchants@1" in failed.output


def test_cli_contract_schema() -> None:
    result = CliRunner().invoke(app, ["contract", "schema"])
    assert result.exit_code == 0
    assert json.loads(result.output)["title"] == "RAI data contract"
