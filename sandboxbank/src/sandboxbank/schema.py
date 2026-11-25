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

# P2's delivery: one load of posted transactions into the analytics warehouse
# (warehouse.fact_transaction, without the warehouse's own loaded_at).
FACT_TRANSACTION: tuple[Column, ...] = (
    Column("business_date", "date", True, "UTC date of created_at"),
    Column("ledger_seq", "integer", True, "The ledger's sequence number; increases"),
    Column("txn_id", "uuid", True, "Primary key; unique within and across loads"),
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
    Column(
        "created_at",
        "timestamp",
        True,
        "UTC, millisecond precision, inside the load's batch window",
    ),
    Column("is_fraud", "boolean", True, "Synthetic fraud label"),
    Column("committed_at", "timestamp", True, "When the ledger committed it; after created_at"),
)

# P3's delivery: one chunk of the fraud feature table (features.fraud_features).
FRAUD_FEATURES: tuple[Column, ...] = (
    Column("txn_id", "uuid", True, "Primary key; one feature row per posted transaction"),
    Column("ledger_seq", "integer", True, "The ledger's sequence number"),
    Column("business_date", "date", True, "UTC date of the transaction"),
    Column("amount", "decimal", True, "Positive, 2 dp"),
    Column("amount_to_limit", "decimal", False, "amount / the account's txn_limit, in (0, 1]"),
    Column("channel", "string", True, "card_present, ecommerce, ach, wire or p2p"),
    Column("credit", "integer", True, "1 for a credit, 0 for a debit"),
    Column("status", "string", True, "posted: the warehouse holds posted transactions only"),
    Column("account_type", "string", False, "checking, savings or business"),
    Column("has_merchant", "integer", True, "1 when the transaction names a merchant, else 0"),
    Column(
        "merchant_category",
        "string",
        False,
        "A category of the merchants catalogue; empty when has_merchant is 0",
    ),
    Column("is_fraud", "boolean", True, "Synthetic fraud label"),
    Column("score", "decimal", True, "The model's fraud probability, in [0, 1], 6 dp"),
    Column("model_recipe_sha256", "string", True, "SHA-256 of the model recipe that scored it"),
    Column(
        "scored_at",
        "timestamp",
        True,
        "When the feature-job scored it: inside the batch window, or at most 10 s after it",
    ),
)

TABLES: dict[str, tuple[Column, ...]] = {
    "accounts": ACCOUNTS,
    "merchants": MERCHANTS,
    "transactions_raw": TRANSACTIONS_RAW,
    "fact_transaction": FACT_TRANSACTION,
    "fraud_features": FRAUD_FEATURES,
}
# The table each pipeline delivers, batch by batch, in the defect study (Protocol 4).
PIPELINE_TABLES: dict[str, str] = {
    "P1": "transactions_raw",
    "P2": "fact_transaction",
    "P3": "fraud_features",
}


def column_names(table: str) -> tuple[str, ...]:
    """Column names of a table, in file order."""
    return tuple(column.name for column in TABLES[table])
