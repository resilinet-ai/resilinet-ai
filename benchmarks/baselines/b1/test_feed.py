"""Arm B1: the conventional test suite for the SandboxBank pipelines' deliveries.

Protocol section 3: "pytest unit and integration tests written from the
pipeline's requirements: required fields, types, a primary-key check per batch".
This is what a small institution typically runs in CI today.

Written from the pipeline specification only (SandboxBank schema v1:
``sandboxbank/README.md`` "Schema v1" and ``sandboxbank/src/sandboxbank/schema.py``),
never from defect fixtures. Each delivered file is tested on its own:

* the columns are the specified ones;
* every required field has a value;
* every value has its specified type;
* the primary key is unique within the file.

The harness runs this file once per delivery (``rai_bench.baselines.B1Arm``):
``RAI_B1_BATCH`` names the batch file, ``RAI_B1_TABLE`` its table (P1
``transactions_raw``, the default; P2 ``fact_transaction``; P3 ``fraud_features``, added
for B-28), and ``RAI_B1_DIMENSION`` the merchants file delivered with it, if any.
Frozen after the external review; do not edit after the freeze (Protocol section 3).
"""

from __future__ import annotations

import csv
import os
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path

import pytest

# Schema v1: column -> (type, required), in file order.
SPEC: dict[str, dict[str, tuple[str, bool]]] = {
    "transactions_raw": {
        "txn_id": ("uuid", True),
        "batch_id": ("string", True),
        "account_id": ("string", True),
        "merchant_id": ("string", False),  # empty for ach, wire and p2p
        "channel": ("string", True),
        "direction": ("string", True),
        "amount": ("decimal", True),
        "currency": ("string", True),
        "status": ("string", True),
        "created_at": ("timestamp", True),
        "is_fraud": ("boolean", True),
    },
    "fact_transaction": {
        "business_date": ("date", True),
        "ledger_seq": ("integer", True),
        "txn_id": ("uuid", True),
        "account_id": ("string", True),
        "merchant_id": ("string", False),  # empty for ach, wire and p2p
        "channel": ("string", True),
        "direction": ("string", True),
        "amount": ("decimal", True),
        "currency": ("string", True),
        "created_at": ("timestamp", True),
        "is_fraud": ("boolean", True),
        "committed_at": ("timestamp", True),
    },
    "fraud_features": {
        "txn_id": ("uuid", True),
        "ledger_seq": ("integer", True),
        "business_date": ("date", True),
        "amount": ("decimal", True),
        "amount_to_limit": ("decimal", False),
        "channel": ("string", True),
        "credit": ("integer", True),
        "status": ("string", True),
        "account_type": ("string", False),
        "has_merchant": ("integer", True),
        "merchant_category": ("string", False),  # empty when has_merchant is 0
        "is_fraud": ("boolean", True),
        "score": ("decimal", True),
        "model_recipe_sha256": ("string", True),
        "scored_at": ("timestamp", True),
    },
    "merchants": {
        "merchant_id": ("string", True),
        "name": ("string", True),
        "mcc": ("string", True),
        "category": ("string", True),
        "country": ("string", True),
    },
}
PRIMARY_KEY = {"transactions_raw": "txn_id", "fact_transaction": "txn_id",
               "fraud_features": "txn_id", "merchants": "merchant_id"}  # fmt: skip
TABLE = os.environ.get("RAI_B1_TABLE") or "transactions_raw"
FILES = {
    TABLE: os.environ.get("RAI_B1_BATCH"),
    "merchants": os.environ.get("RAI_B1_DIMENSION"),
}
DELIVERED = [table for table, path in FILES.items() if path]


@cache
def load(table: str) -> tuple[list[str], list[dict[str, str]]]:
    path = Path(FILES[table] or "")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def parses(kind: str, value: str) -> bool:
    try:
        if kind == "uuid":
            uuid.UUID(value)
        elif kind == "decimal":
            return Decimal(value).is_finite()
        elif kind == "timestamp":
            return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
        elif kind == "date":
            date.fromisoformat(value)
        elif kind == "integer":
            int(value)
        elif kind == "boolean":
            return value in ("true", "false")
    except (ValueError, InvalidOperation):
        return False
    return True


def columns(kind: str | None = None, *, required: bool | None = None) -> list[tuple[str, str]]:
    return [
        (table, column)
        for table in DELIVERED
        for column, (column_kind, is_required) in SPEC[table].items()
        if (kind is None or (column_kind != "string") == (kind == "typed"))
        and (required is None or is_required == required)
    ]


def ids(params: list[tuple[str, str]]) -> list[str]:
    return [f"{table}-{column}" for table, column in params]


@pytest.mark.parametrize("table", DELIVERED)
def test_columns(table: str) -> None:
    header, _ = load(table)
    missing = [c for c in SPEC[table] if c not in header]
    unexpected = [c for c in header if c not in SPEC[table]]
    assert missing + unexpected == [], f"missing: {missing}; unexpected: {unexpected}"


@pytest.mark.parametrize(
    ("table", "column"), columns(required=True), ids=ids(columns(required=True))
)
def test_required(table: str, column: str) -> None:
    header, rows = load(table)
    if column not in header:
        pytest.skip("column missing: reported by test_columns")
    empty = sum(1 for row in rows if not (row[column] or "").strip())
    assert empty == 0, f"{column}: {empty} of {len(rows)} rows have no value"


@pytest.mark.parametrize(("table", "column"), columns("typed"), ids=ids(columns("typed")))
def test_type(table: str, column: str) -> None:
    header, rows = load(table)
    if column not in header:
        pytest.skip("column missing: reported by test_columns")
    kind = SPEC[table][column][0]
    bad = sum(1 for row in rows if (row[column] or "").strip() and not parses(kind, row[column]))
    assert bad == 0, f"{column}: {bad} of {len(rows)} rows are not a valid {kind}"


@pytest.mark.parametrize("table", DELIVERED)
def test_primary_key(table: str) -> None:
    header, rows = load(table)
    key = PRIMARY_KEY[table]
    if key not in header:
        pytest.skip("column missing: reported by test_columns")
    values = [row[key] for row in rows if row[key]]
    repeated = len(values) - len(set(values))
    assert repeated == 0, f"{key}: {repeated} of {len(rows)} rows repeat a key in this file"
