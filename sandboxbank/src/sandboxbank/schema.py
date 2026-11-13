"""SandboxBank table schemas, schema version 1 (decision log: SBX-SCHEMA).

These are the columns the RAI data contracts, the defect injector (B-07) and the
benchmark baselines are written against. Changing a column here is a breaking
change: bump SCHEMA_VERSION and record it in docs/decision-log.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SCHEMA_VERSION = "1"

ColumnType = Literal["string", "uuid", "decimal", "integer", "boolean", "date", "timestamp"]


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: ColumnType
    required: bool
    description: str


ACCOUNTS: tuple[Column, ...] = (
    Column("account_id", "string", True, "Primary key, ACC- followed by 7 digits"),
    Column("customer_id", "string", True, "CUS- followed by 7 digits; a customer may own several"),
    Column("account_type", "string", True, "checking, savings or business"),
    Column("currency", "string", True, "ISO 4217 code; always USD in schema v1"),
    Column("status", "string", True, "active, dormant or closed; only active accounts transact"),
    Column("opened_on", "date", True, "Date the account was opened"),
    Column("txn_limit", "decimal", True, "Maximum single transaction amount, 2 dp"),
)

MERCHANTS: tuple[Column, ...] = (
    Column("merchant_id", "string", True, "Primary key, MER- followed by 6 digits"),
    Column("name", "string", True, "Synthetic name, never a real business"),
    Column("mcc", "string", True, "4-digit merchant category code"),
    Column("category", "string", True, "Human-readable category for the MCC"),
    Column("country", "string", True, "ISO 3166-1 alpha-2 code"),
)

TRANSACTIONS_RAW: tuple[Column, ...] = (
    Column("txn_id", "uuid", True, "Primary key; unique within and across batches"),
    Column("batch_id", "string", True, "B followed by 6 digits"),
    Column("account_id", "string", True, "References accounts.account_id"),
    Column(
        "merchant_id",
        "string",
        False,
        "References merchants.merchant_id; empty for ach, wire and p2p channels",
    ),
    Column("channel", "string", True, "card_present, ecommerce, ach, wire or p2p"),
    Column("direction", "string", True, "debit or credit"),
    Column("amount", "decimal", True, "Positive, 2 dp, at most the account txn_limit"),
    Column("currency", "string", True, "ISO 4217 code; always USD in schema v1"),
    Column("status", "string", True, "posted, pending or declined"),
    Column("created_at", "timestamp", True, "UTC, millisecond precision, inside the batch window"),
    Column("is_fraud", "boolean", True, "Synthetic fraud label for the P3 feature pipeline"),
)

TABLES: dict[str, tuple[Column, ...]] = {
    "accounts": ACCOUNTS,
    "merchants": MERCHANTS,
    "transactions_raw": TRANSACTIONS_RAW,
}


def column_names(table: str) -> tuple[str, ...]:
    """Column names of a table, in file order."""
    return tuple(column.name for column in TABLES[table])
