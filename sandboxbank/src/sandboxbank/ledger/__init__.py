"""SandboxBank ledger: schema, request model and idempotent posting (B-03).

Posting rules (decision log: SBX-LEDGER):

* ``txn_id`` is the idempotency key. Re-posting an identical request is a
  *replay* and returns the original ledger row; re-posting the same ``txn_id``
  with different content is a *conflict* and changes nothing.
* Business-rule failures (unknown or inactive account, amount over the account
  limit, unknown merchant) *reject* the row without touching the ledger.
* The ledger is append-only, enforced by a database trigger.

Every outcome is decided inside one database transaction, and a lost insert race
is resolved by re-reading the winner, so concurrent retries of the same
``txn_id`` create exactly one ledger row.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from typing import Any, Literal, Self
from uuid import UUID

import psycopg
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from sandboxbank.generator import MERCHANT_CHANNELS

Outcome = Literal["created", "replayed", "conflict", "rejected"]
MAX_BATCH = 1_000
CENT = Decimal("0.01")


def schema_sql() -> str:
    return resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")


def migrate(conn: psycopg.Connection[Any]) -> None:
    """Create or update the ledger schema; idempotent."""
    with conn.transaction():
        conn.execute(schema_sql())  # trusted packaged SQL


class TransactionIn(BaseModel):
    """One ``transactions_raw`` row as posted to the payments API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    txn_id: UUID
    batch_id: str = Field(pattern=r"^B\d{6}$")
    account_id: str = Field(pattern=r"^ACC-\d{7}$")
    merchant_id: str | None = Field(default=None, pattern=r"^MER-\d{6}$")
    channel: Literal["card_present", "ecommerce", "ach", "wire", "p2p"]
    direction: Literal["debit", "credit"]
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    status: Literal["posted", "pending", "declined"]
    created_at: AwareDatetime
    is_fraud: bool

    @field_validator("amount")
    @classmethod
    def _two_places(cls, value: Decimal) -> Decimal:
        return value.quantize(CENT)

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _merchant_matches_channel(self) -> Self:
        needs_merchant = self.channel in MERCHANT_CHANNELS
        if needs_merchant and self.merchant_id is None:
            raise ValueError(f"merchant_id is required for channel {self.channel}")
        if not needs_merchant and self.merchant_id is not None:
            raise ValueError(f"merchant_id must be empty for channel {self.channel}")
        return self

    def request_sha256(self) -> str:
        """Fingerprint of the normalised request, used to tell replays from conflicts."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PostResult:
    txn_id: UUID
    outcome: Outcome
    seq: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Existing:
    sha256: str
    seq: int


def _existing(conn: psycopg.Connection[Any], ids: Sequence[UUID]) -> dict[UUID, _Existing]:
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT txn_id, request_sha256, seq FROM ledger_transactions WHERE txn_id = ANY(%s)",
        (list(ids),),
    ).fetchall()
    return {row[0]: _Existing(row[1], row[2]) for row in rows}


def _compare(txn: TransactionIn, sha: str, found: _Existing) -> PostResult:
    if found.sha256 == sha:
        return PostResult(txn.txn_id, "replayed", found.seq)
    return PostResult(
        txn.txn_id, "conflict", found.seq, "txn_id already posted with different content"
    )


def _rule_failure(
    txn: TransactionIn,
    accounts: dict[str, tuple[str, Decimal]],
    merchants: set[str],
) -> str | None:
    account = accounts.get(txn.account_id)
    if account is None:
        return "unknown account_id"
    status, limit = account
    if status != "active":
        return f"account is {status}"
    if txn.amount > limit:
        return "amount exceeds account txn_limit"
    if txn.merchant_id is not None and txn.merchant_id not in merchants:
        return "unknown merchant_id"
    return None


_INSERT = """
INSERT INTO ledger_transactions (
    txn_id, batch_id, account_id, merchant_id, channel, direction, amount,
    currency, status, created_at, is_fraud, request_sha256
)
SELECT * FROM unnest(
    %s::uuid[], %s::text[], %s::text[], %s::text[], %s::text[], %s::text[],
    %s::numeric[], %s::text[], %s::text[], %s::timestamptz[], %s::boolean[], %s::text[]
)
ON CONFLICT (txn_id) DO NOTHING
RETURNING txn_id, seq
"""


def post_transactions(
    conn: psycopg.Connection[Any], transactions: Sequence[TransactionIn]
) -> list[PostResult]:
    """Post a batch idempotently; one result per input, in input order."""
    if len(transactions) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} transactions per call")
    hashes = [txn.request_sha256() for txn in transactions]
    decided: dict[int, PostResult] = {}

    with conn.transaction():
        # Repeats inside the same request are judged against the first occurrence.
        first_seen: dict[UUID, int] = {}
        for index, txn in enumerate(transactions):
            first_seen.setdefault(txn.txn_id, index)

        existing = _existing(conn, list(first_seen))
        account_ids = list({txn.account_id for txn in transactions})
        accounts = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT account_id, status, txn_limit FROM accounts WHERE account_id = ANY(%s)",
                (account_ids,),
            )
        }
        merchant_ids = list({txn.merchant_id for txn in transactions if txn.merchant_id})
        merchants = {
            row[0]
            for row in conn.execute(
                "SELECT merchant_id FROM merchants WHERE merchant_id = ANY(%s)", (merchant_ids,)
            )
        }

        to_insert: list[int] = []
        for index in first_seen.values():
            txn = transactions[index]
            if txn.txn_id in existing:
                decided[index] = _compare(txn, hashes[index], existing[txn.txn_id])
            elif (reason := _rule_failure(txn, accounts, merchants)) is not None:
                decided[index] = PostResult(txn.txn_id, "rejected", None, reason)
            else:
                to_insert.append(index)

        if to_insert:
            rows = [transactions[i] for i in to_insert]
            inserted = {
                row[0]: row[1]
                for row in conn.execute(
                    _INSERT,
                    (
                        [t.txn_id for t in rows],
                        [t.batch_id for t in rows],
                        [t.account_id for t in rows],
                        [t.merchant_id for t in rows],
                        [t.channel for t in rows],
                        [t.direction for t in rows],
                        [t.amount for t in rows],
                        [t.currency for t in rows],
                        [t.status for t in rows],
                        [t.created_at for t in rows],
                        [t.is_fraud for t in rows],
                        [hashes[i] for i in to_insert],
                    ),
                )
            }
            lost = [
                transactions[i].txn_id for i in to_insert if transactions[i].txn_id not in inserted
            ]
            winners = _existing(conn, lost)  # a concurrent request committed first
            for index in to_insert:
                txn = transactions[index]
                if txn.txn_id in inserted:
                    decided[index] = PostResult(txn.txn_id, "created", inserted[txn.txn_id])
                else:
                    decided[index] = _compare(txn, hashes[index], winners[txn.txn_id])

    return [_result_for(i, transactions, hashes, first_seen, decided) for i in range(len(hashes))]


def _result_for(
    index: int,
    transactions: Sequence[TransactionIn],
    hashes: Sequence[str],
    first_seen: dict[UUID, int],
    decided: dict[int, PostResult],
) -> PostResult:
    """Result for one input; a repeat inside the request follows its first occurrence."""
    if index in decided:
        return decided[index]
    txn = transactions[index]
    first = first_seen[txn.txn_id]
    original = decided[first]
    if hashes[index] != hashes[first]:
        return PostResult(
            txn.txn_id, "conflict", original.seq, "txn_id repeated with different content"
        )
    outcome: Outcome = (
        "replayed" if original.outcome in {"created", "replayed"} else original.outcome
    )
    return PostResult(txn.txn_id, outcome, original.seq, original.reason)


@dataclass(frozen=True, slots=True)
class LedgerHead:
    seq: int | None
    committed_at: datetime | None
    count: int


def ledger_head(conn: psycopg.Connection[Any]) -> LedgerHead:
    """Last committed ledger record: the anchor for RPO measurement (FR-08.2)."""
    row = conn.execute(
        "SELECT seq, committed_at, (SELECT count(*) FROM ledger_transactions) "
        "FROM ledger_transactions ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return LedgerHead(None, None, 0)
    return LedgerHead(row[0], row[1], row[2])


def get_transaction(conn: psycopg.Connection[Any], txn_id: UUID) -> dict[str, Any] | None:
    cursor = conn.execute(
        "SELECT seq, txn_id, batch_id, account_id, merchant_id, channel, direction, amount, "
        "currency, status, created_at, is_fraud, committed_at "
        "FROM ledger_transactions WHERE txn_id = %s",
        (txn_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    names = [column.name for column in cursor.description or []]
    return dict(zip(names, row, strict=True))
