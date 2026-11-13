"""Seeded synthetic data for SandboxBank: accounts, merchants and transaction batches.

Reproducibility rules (Spec section 5, NFR-REP-1):

* Every random stream comes from ``random.Random`` seeded by a SHA-256 derivation of
  the master seed and a stream name, so each batch can be regenerated on its own.
* Only integer and table-driven draws are used (no log/exp/trig), so the same seed
  gives byte-identical files on every platform and Python 3.12+ build.
* No wall-clock time, hostnames or other ambient state enters the data.

All data is synthetic. Names and identifiers are generated, never sampled from
real people or businesses.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import accumulate
from typing import Literal

from sandboxbank.schema import SCHEMA_VERSION

DEV_SEEDS = range(0, 100_000)
EVAL_SEEDS = range(100_000, 200_000)

SeedPurpose = Literal["development", "evaluation"]

DEFAULT_START = datetime(2026, 1, 1, tzinfo=UTC)
FIRST_OPENED_ON = date(2015, 1, 1)
CURRENCY = "USD"
FRAUD_RATE = 0.002

# ---- distributions (weights are relative) ---------------------------------

ACCOUNT_TYPES: Sequence[tuple[str, int]] = (("checking", 60), ("savings", 25), ("business", 15))
ACCOUNT_STATUS: Sequence[tuple[str, int]] = (("active", 92), ("dormant", 6), ("closed", 2))
TXN_LIMIT_CENTS = {"checking": 500_000, "savings": 250_000, "business": 2_500_000}
ACTIVITY_WEIGHTS: Sequence[tuple[int, int]] = ((1, 50), (2, 30), (5, 15), (20, 5))
SHARED_CUSTOMER_RATE = 0.2

MCC_CATEGORIES: Sequence[tuple[str, str, int]] = (
    ("5411", "grocery", 20),
    ("5812", "restaurants", 18),
    ("5541", "fuel", 10),
    ("5999", "retail", 15),
    ("4111", "transport", 8),
    ("4900", "utilities", 6),
    ("5732", "electronics", 7),
    ("7011", "hotels", 5),
    ("5967", "direct_marketing", 3),
    ("6051", "quasi_cash", 2),
    ("7995", "gambling", 2),
)
HIGH_RISK_MCCS = frozenset({"5967", "6051", "5732", "7995"})
MERCHANT_COUNTRIES: Sequence[tuple[str, int]] = (
    ("US", 90),
    ("CA", 3),
    ("GB", 3),
    ("DE", 2),
    ("NG", 1),
    ("BR", 1),
)

CHANNELS_BY_ACCOUNT_TYPE: dict[str, Sequence[tuple[str, int]]] = {
    "checking": (("card_present", 45), ("ecommerce", 30), ("ach", 12), ("wire", 1), ("p2p", 12)),
    "savings": (("ach", 70), ("wire", 5), ("p2p", 25)),
    "business": (("card_present", 15), ("ecommerce", 20), ("ach", 40), ("wire", 20), ("p2p", 5)),
}
FRAUD_CHANNELS: Sequence[tuple[str, int]] = (("ecommerce", 70), ("card_present", 20), ("p2p", 10))
MERCHANT_CHANNELS = frozenset({"card_present", "ecommerce"})

# Amount buckets in cents: (low inclusive, high exclusive, weight).
AmountBuckets = Sequence[tuple[int, int, int]]
AMOUNTS_BY_CHANNEL: dict[str, AmountBuckets] = {
    "card_present": (
        (100, 2_000, 50),
        (2_000, 10_000, 35),
        (10_000, 50_000, 13),
        (50_000, 200_000, 2),
    ),
    "ecommerce": (
        (100, 2_000, 30),
        (2_000, 10_000, 40),
        (10_000, 50_000, 25),
        (50_000, 300_000, 5),
    ),
    "ach": ((5_000, 50_000, 40), (50_000, 250_000, 45), (250_000, 1_000_000, 15)),
    "wire": ((50_000, 500_000, 60), (500_000, 2_500_000, 40)),
    "p2p": ((500, 5_000, 50), (5_000, 25_000, 40), (25_000, 100_000, 10)),
}
FRAUD_AMOUNTS: AmountBuckets = ((20_000, 100_000, 50), (100_000, 500_000, 50))

# Probability (out of 1000) that a transaction is a credit, by channel.
CREDIT_PER_MILLE = {"card_present": 20, "ecommerce": 20, "ach": 500, "wire": 300, "p2p": 500}
TXN_STATUS: Sequence[tuple[str, int]] = (("posted", 95), ("pending", 4), ("declined", 1))
FRAUD_TXN_STATUS: Sequence[tuple[str, int]] = (("posted", 65), ("pending", 5), ("declined", 30))


# ---- configuration --------------------------------------------------------


def seed_purpose(seed: int) -> SeedPurpose:
    """Classify a seed by the protocol's ranges; anything else is rejected.

    Development uses 0-99,999 and evaluation 100,000-199,999 (Protocol, reproducibility),
    so evaluation fixtures can never be produced by accident during development.
    """
    if seed in DEV_SEEDS:
        return "development"
    if seed in EVAL_SEEDS:
        return "evaluation"
    raise ValueError(f"seed {seed} is outside the protocol ranges 0-99999 and 100000-199999")


@dataclass(frozen=True, slots=True)
class GeneratorConfig:
    seed: int
    accounts: int = 5_000
    merchants: int = 500
    batches: int = 1
    rows_per_batch: int = 100_000
    batch_window_seconds: int = 900
    start: datetime = DEFAULT_START

    def __post_init__(self) -> None:
        seed_purpose(self.seed)
        for name in ("accounts", "merchants", "batches", "rows_per_batch", "batch_window_seconds"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.start.tzinfo is None or self.start.utcoffset() != timedelta(0):
            raise ValueError("start must be a UTC datetime")
        if self.start.date() <= FIRST_OPENED_ON:
            raise ValueError(f"start must be after {FIRST_OPENED_ON.isoformat()}")

    @property
    def purpose(self) -> SeedPurpose:
        return seed_purpose(self.seed)

    def batch_start(self, batch_index: int) -> datetime:
        return self.start + timedelta(seconds=self.batch_window_seconds * batch_index)


# ---- records --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    customer_id: str
    account_type: str
    currency: str
    status: str
    opened_on: date
    txn_limit_cents: int
    activity: int

    def as_row(self) -> tuple[str, ...]:
        return (
            self.account_id,
            self.customer_id,
            self.account_type,
            self.currency,
            self.status,
            self.opened_on.isoformat(),
            format_cents(self.txn_limit_cents),
        )


@dataclass(frozen=True, slots=True)
class Merchant:
    merchant_id: str
    name: str
    mcc: str
    category: str
    country: str

    def as_row(self) -> tuple[str, ...]:
        return (self.merchant_id, self.name, self.mcc, self.category, self.country)


@dataclass(frozen=True, slots=True)
class Transaction:
    txn_id: str
    batch_id: str
    account_id: str
    merchant_id: str | None
    channel: str
    direction: str
    amount_cents: int
    currency: str
    status: str
    created_at: datetime
    is_fraud: bool

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_cents).scaleb(-2)

    def as_row(self) -> tuple[str, ...]:
        return (
            self.txn_id,
            self.batch_id,
            self.account_id,
            self.merchant_id or "",
            self.channel,
            self.direction,
            format_cents(self.amount_cents),
            self.currency,
            self.status,
            format_timestamp(self.created_at),
            "true" if self.is_fraud else "false",
        )


# ---- helpers --------------------------------------------------------------


def derive_seed(seed: int, stream: str) -> int:
    """Independent 64-bit seed for one named stream of the master seed."""
    material = f"sandboxbank|schema-v{SCHEMA_VERSION}|{seed}|{stream}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def stream(seed: int, name: str) -> random.Random:
    return random.Random(derive_seed(seed, name))  # noqa: S311 - synthetic data, not crypto


def format_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def batch_id(batch_index: int) -> str:
    return f"B{batch_index:06d}"


class _Picker[T]:
    """Weighted choice with precomputed cumulative weights (integer weights only)."""

    def __init__(self, options: Sequence[tuple[T, int]]) -> None:
        self.values = [value for value, _ in options]
        self.cum_weights = list(accumulate(weight for _, weight in options))

    def pick(self, rng: random.Random) -> T:
        return rng.choices(self.values, cum_weights=self.cum_weights)[0]


class _AmountPicker:
    """Pick a bucket by weight, then a whole number of cents uniformly inside it."""

    def __init__(self, buckets: AmountBuckets) -> None:
        self._buckets = _Picker([((low, high), weight) for low, high, weight in buckets])

    def pick(self, rng: random.Random) -> int:
        low, high = self._buckets.pick(rng)
        return rng.randrange(low, high)


# ---- generation -----------------------------------------------------------


def generate_accounts(config: GeneratorConfig) -> list[Account]:
    rng = stream(config.seed, "accounts")
    types = _Picker(ACCOUNT_TYPES)
    statuses = _Picker(ACCOUNT_STATUS)
    activity = _Picker(ACTIVITY_WEIGHTS)
    opening_days = (config.start.date() - FIRST_OPENED_ON).days
    accounts: list[Account] = []
    customer = 0
    for number in range(1, config.accounts + 1):
        if customer == 0 or rng.random() >= SHARED_CUSTOMER_RATE:
            customer += 1
        account_type = types.pick(rng)
        accounts.append(
            Account(
                account_id=f"ACC-{number:07d}",
                customer_id=f"CUS-{customer:07d}",
                account_type=account_type,
                currency=CURRENCY,
                status=statuses.pick(rng),
                opened_on=FIRST_OPENED_ON + timedelta(days=rng.randrange(opening_days)),
                txn_limit_cents=TXN_LIMIT_CENTS[account_type],
                activity=activity.pick(rng),
            )
        )
    return accounts


def generate_merchants(config: GeneratorConfig) -> list[Merchant]:
    rng = stream(config.seed, "merchants")
    mccs = _Picker([((mcc, category), weight) for mcc, category, weight in MCC_CATEGORIES])
    countries = _Picker(MERCHANT_COUNTRIES)
    merchants = []
    for number in range(1, config.merchants + 1):
        mcc, category = mccs.pick(rng)
        merchants.append(
            Merchant(
                merchant_id=f"MER-{number:06d}",
                name=f"SBX Merchant {number:06d}",
                mcc=mcc,
                category=category,
                country=countries.pick(rng),
            )
        )
    return merchants


def generate_batch(
    config: GeneratorConfig,
    accounts: Sequence[Account],
    merchants: Sequence[Merchant],
    batch_index: int,
) -> list[Transaction]:
    """One micro-batch of ``transactions_raw``, sorted by created_at then txn_id.

    Depends only on the config, the reference tables and ``batch_index``, so any
    batch can be regenerated without generating the ones before it.
    """
    if not 0 <= batch_index < config.batches:
        raise ValueError(f"batch_index must be in 0..{config.batches - 1}")
    active = [account for account in accounts if account.status == "active"]
    if not active:
        raise ValueError("no active accounts to transact on")
    if not merchants:
        raise ValueError("at least one merchant is required")

    rng = stream(config.seed, f"transactions/{batch_index}")
    account_cum = list(accumulate(account.activity for account in active))
    risky = [merchant for merchant in merchants if merchant.mcc in HIGH_RISK_MCCS] or list(
        merchants
    )
    channel_pickers = {kind: _Picker(options) for kind, options in CHANNELS_BY_ACCOUNT_TYPE.items()}
    fraud_channels = _Picker(FRAUD_CHANNELS)
    statuses = _Picker(TXN_STATUS)
    fraud_statuses = _Picker(FRAUD_TXN_STATUS)
    amounts = {channel: _AmountPicker(buckets) for channel, buckets in AMOUNTS_BY_CHANNEL.items()}
    fraud_amounts = _AmountPicker(FRAUD_AMOUNTS)
    window_ms = config.batch_window_seconds * 1000
    window_start = config.batch_start(batch_index)
    this_batch = batch_id(batch_index)

    rows: list[Transaction] = []
    for _ in range(config.rows_per_batch):
        account = rng.choices(active, cum_weights=account_cum)[0]
        is_fraud = rng.random() < FRAUD_RATE
        if is_fraud:
            channel = fraud_channels.pick(rng)
            if account.account_type == "savings":
                channel = "p2p"
            amount = fraud_amounts.pick(rng)
            direction = "debit"
            status = fraud_statuses.pick(rng)
            pool: Sequence[Merchant] = risky if rng.random() < 0.8 else merchants
        else:
            channel = channel_pickers[account.account_type].pick(rng)
            amount = amounts[channel].pick(rng)
            direction = "credit" if rng.randrange(1000) < CREDIT_PER_MILLE[channel] else "debit"
            status = statuses.pick(rng)
            pool = merchants
        if amount > account.txn_limit_cents:
            amount = rng.randrange(100, account.txn_limit_cents + 1)
        merchant_id = rng.choice(pool).merchant_id if channel in MERCHANT_CHANNELS else None
        rows.append(
            Transaction(
                txn_id=str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                batch_id=this_batch,
                account_id=account.account_id,
                merchant_id=merchant_id,
                channel=channel,
                direction=direction,
                amount_cents=amount,
                currency=CURRENCY,
                status=status,
                created_at=window_start + timedelta(milliseconds=rng.randrange(window_ms)),
                is_fraud=is_fraud,
            )
        )
    rows.sort(key=lambda txn: (txn.created_at, txn.txn_id))
    return rows
