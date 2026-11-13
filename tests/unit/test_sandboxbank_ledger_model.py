"""Payments-api request model and loader helpers; no database needed (B-03)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sandboxbank.cli import main
from sandboxbank.ledger import TransactionIn, schema_sql
from sandboxbank.loader import post_batches, to_payload


def payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "txn_id": "196c1a05-f633-49ec-a32d-c3901c963dda",
        "batch_id": "B000000",
        "account_id": "ACC-0000001",
        "merchant_id": "MER-000001",
        "channel": "ecommerce",
        "direction": "debit",
        "amount": "89.74",
        "currency": "USD",
        "status": "posted",
        "created_at": "2026-01-01T00:00:00.010Z",
        "is_fraud": False,
    }
    return base | overrides


def test_valid_payload_is_accepted() -> None:
    txn = TransactionIn.model_validate(payload())
    assert txn.amount == Decimal("89.74")
    assert txn.created_at.tzinfo is not None


def test_equivalent_requests_have_the_same_fingerprint() -> None:
    plain = TransactionIn.model_validate(payload(amount="12.3"))
    padded = TransactionIn.model_validate(payload(amount="12.30"))
    offset = TransactionIn.model_validate(payload(created_at="2026-01-01T02:00:00.010+02:00"))
    utc = TransactionIn.model_validate(payload(created_at="2026-01-01T00:00:00.010Z"))
    assert plain.request_sha256() == padded.request_sha256()
    assert offset.request_sha256() == utc.request_sha256()
    assert offset.created_at == datetime(2026, 1, 1, 0, 0, 0, 10_000, tzinfo=UTC)


def test_different_content_has_a_different_fingerprint() -> None:
    a = TransactionIn.model_validate(payload())
    b = TransactionIn.model_validate(payload(amount="89.75"))
    assert a.request_sha256() != b.request_sha256()


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": "0"},
        {"amount": "-1.00"},
        {"amount": "1.001"},
        {"amount": "abc"},
        {"currency": "usd"},
        {"channel": "fax"},
        {"account_id": "ACC-1"},
        {"created_at": "2026-01-01T00:00:00"},
        {"merchant_id": None},
        {"channel": "ach"},
        {"unexpected": "field"},
    ],
)
def test_invalid_payloads_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TransactionIn.model_validate(payload(**overrides))


def test_non_merchant_channel_without_merchant_is_valid() -> None:
    txn = TransactionIn.model_validate(payload(channel="wire", merchant_id=None))
    assert txn.merchant_id is None
    assert txn.created_at.utcoffset() == timedelta(0)


def test_csv_row_becomes_api_payload() -> None:
    row = {k: str(v) for k, v in payload(merchant_id="", channel="ach", is_fraud="true").items()}
    converted = to_payload(row)
    assert converted["merchant_id"] is None
    assert converted["is_fraud"] is True
    TransactionIn.model_validate(converted)


def test_schema_is_append_only_and_idempotent_sql() -> None:
    sql = schema_sql()
    assert "CREATE TABLE IF NOT EXISTS ledger_transactions" in sql
    assert "BEFORE UPDATE OR DELETE ON ledger_transactions" in sql


@pytest.mark.parametrize(
    ("url", "chunk", "message"),
    [
        ("file:///etc/passwd", 10, "http or https"),
        ("http://x", 0, "chunk"),
        ("http://x", 1001, "chunk"),
    ],
)
def test_post_batches_validates_arguments(url: str, chunk: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        post_batches(url, [], chunk=chunk)


def test_cli_load_reference_needs_dsn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SANDBOXBANK_LEDGER_DSN", raising=False)
    assert main(["load-reference", "--data", str(tmp_path)]) == 2
    assert "SANDBOXBANK_LEDGER_DSN" in capsys.readouterr().err


def test_cli_post_reports_missing_batches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["post", "--data", str(tmp_path), "--batch", "B000009"]) == 2
    assert "B000009.csv" in capsys.readouterr().err
    assert main(["post", "--data", str(tmp_path)]) == 2


def test_cli_post_reports_bad_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "transactions_raw"
    folder.mkdir()
    (folder / "B000000.csv").write_text("txn_id\n", encoding="utf-8")
    assert main(["post", "--data", str(tmp_path), "--api", "ftp://nope"]) == 2
    assert "http or https" in capsys.readouterr().err
