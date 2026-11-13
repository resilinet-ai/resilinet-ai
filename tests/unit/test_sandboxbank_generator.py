"""SandboxBank generator: reproducibility (NFR-REP-1) and data invariants (B-02)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sandboxbank.generator import (
    Account,
    GeneratorConfig,
    Merchant,
    derive_seed,
    format_cents,
    format_timestamp,
    generate_accounts,
    generate_batch,
    generate_merchants,
    seed_purpose,
)
from sandboxbank.schema import TABLES, column_names

Reference = tuple[list[Account], list[Merchant]]

SMALL = GeneratorConfig(seed=7, accounts=200, merchants=40, batches=3, rows_per_batch=2_000)


@pytest.fixture(scope="module")
def reference() -> Reference:
    return generate_accounts(SMALL), generate_merchants(SMALL)


# ---- seeds and configuration ------------------------------------------------


@pytest.mark.parametrize(
    ("seed", "purpose"),
    [(0, "development"), (99_999, "development"), (100_000, "evaluation"), (199_999, "evaluation")],
)
def test_seed_ranges_follow_the_protocol(seed: int, purpose: str) -> None:
    assert seed_purpose(seed) == purpose


@pytest.mark.parametrize("seed", [-1, 200_000])
def test_seeds_outside_the_protocol_ranges_are_rejected(seed: int) -> None:
    with pytest.raises(ValueError, match="outside the protocol ranges"):
        GeneratorConfig(seed=seed)


@pytest.mark.parametrize(
    "overrides",
    [
        {"accounts": 0},
        {"rows_per_batch": 0},
        {"start": datetime(2026, 1, 1)},  # naive on purpose
        {"start": datetime(2014, 1, 1, tzinfo=UTC)},
    ],
)
def test_invalid_config_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must be"):
        GeneratorConfig(seed=1, **overrides)  # type: ignore[arg-type]


def test_streams_are_independent_per_name_and_seed() -> None:
    assert derive_seed(1, "accounts") != derive_seed(1, "merchants")
    assert derive_seed(1, "accounts") != derive_seed(2, "accounts")
    assert derive_seed(1, "accounts") == derive_seed(1, "accounts")


# ---- reproducibility --------------------------------------------------------


def test_same_seed_gives_identical_data(reference: Reference) -> None:
    accounts, merchants = reference
    assert generate_accounts(SMALL) == accounts
    assert generate_merchants(SMALL) == merchants
    assert generate_batch(SMALL, accounts, merchants, 1) == generate_batch(
        SMALL, accounts, merchants, 1
    )


def test_different_seeds_give_different_data() -> None:
    other = GeneratorConfig(seed=8, accounts=200, merchants=40, batches=3, rows_per_batch=2_000)
    assert generate_accounts(other) != generate_accounts(SMALL)


def test_a_batch_does_not_depend_on_how_many_batches_are_requested(
    reference: Reference,
) -> None:
    accounts, merchants = reference
    fewer = GeneratorConfig(seed=7, accounts=200, merchants=40, batches=2, rows_per_batch=2_000)
    assert generate_batch(fewer, accounts, merchants, 1) == generate_batch(
        SMALL, accounts, merchants, 1
    )


def test_batch_index_out_of_range_is_rejected(reference: Reference) -> None:
    accounts, merchants = reference
    with pytest.raises(ValueError, match="batch_index"):
        generate_batch(SMALL, accounts, merchants, 3)


def test_batch_needs_active_accounts_and_merchants(reference: Reference) -> None:
    accounts, merchants = reference
    with pytest.raises(ValueError, match="active accounts"):
        generate_batch(SMALL, [], merchants, 0)
    with pytest.raises(ValueError, match="merchant"):
        generate_batch(SMALL, accounts, [], 0)


# ---- invariants the defect catalogue relies on ------------------------------


def test_reference_tables_have_unique_keys(reference: Reference) -> None:
    accounts, merchants = reference
    assert len({a.account_id for a in accounts}) == len(accounts) == SMALL.accounts
    assert len({m.merchant_id for m in merchants}) == len(merchants) == SMALL.merchants
    assert all(a.opened_on < SMALL.start.date() for a in accounts)


def test_transactions_are_clean_and_consistent(reference: Reference) -> None:
    accounts, merchants = reference
    by_id = {a.account_id: a for a in accounts}
    merchant_ids = {m.merchant_id for m in merchants}
    batches = [generate_batch(SMALL, accounts, merchants, i) for i in range(SMALL.batches)]
    every = [txn for batch in batches for txn in batch]

    # D01/D02 baseline: txn_id unique within and across batches.
    assert len({txn.txn_id for txn in every}) == len(every)
    for index, batch in enumerate(batches):
        window_start = SMALL.batch_start(index)
        window_end = window_start + timedelta(seconds=SMALL.batch_window_seconds)
        assert len(batch) == SMALL.rows_per_batch
        assert batch == sorted(batch, key=lambda txn: (txn.created_at, txn.txn_id))
        for txn in batch:
            account = by_id[txn.account_id]  # D06 baseline: every account exists
            assert account.status == "active"
            assert 0 < txn.amount_cents <= account.txn_limit_cents  # D05 baseline
            assert window_start <= txn.created_at < window_end  # D10 baseline
            assert txn.batch_id == f"B{index:06d}"
            if txn.channel in {"card_present", "ecommerce"}:
                assert txn.merchant_id in merchant_ids
            else:
                assert txn.merchant_id is None


def test_fraud_label_is_rare_but_present() -> None:
    config = GeneratorConfig(seed=11, rows_per_batch=50_000)
    batch = generate_batch(config, generate_accounts(config), generate_merchants(config), 0)
    rate = sum(txn.is_fraud for txn in batch) / len(batch)
    assert 0.001 < rate < 0.003


def test_rows_match_the_schema_column_order(reference: Reference) -> None:
    accounts, merchants = reference
    txn = generate_batch(SMALL, accounts, merchants, 0)[0]
    assert len(accounts[0].as_row()) == len(column_names("accounts"))
    assert len(merchants[0].as_row()) == len(column_names("merchants"))
    row = dict(zip(column_names("transactions_raw"), txn.as_row(), strict=True))
    assert row["txn_id"] == txn.txn_id
    assert Decimal(row["amount"]) == txn.amount
    assert row["is_fraud"] in {"true", "false"}
    assert set(TABLES) == {"accounts", "merchants", "transactions_raw"}


def test_formatting_is_exact() -> None:
    assert format_cents(5) == "0.05"
    assert format_cents(123_456) == "1234.56"
    moment = datetime(2026, 1, 1, 0, 0, 1, 23_000, tzinfo=UTC)
    assert format_timestamp(moment) == "2026-01-01T00:00:01.023Z"


@settings(max_examples=25, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=199_999),
    accounts=st.integers(min_value=1, max_value=30),
    rows=st.integers(min_value=1, max_value=300),
)
def test_any_seed_produces_valid_batches(seed: int, accounts: int, rows: int) -> None:
    config = GeneratorConfig(seed=seed, accounts=accounts, merchants=5, rows_per_batch=rows)
    account_rows = generate_accounts(config)
    active = {a.account_id: a for a in account_rows if a.status == "active"}
    if not active:
        return
    batch = generate_batch(config, account_rows, generate_merchants(config), 0)
    assert len(batch) == rows
    assert all(0 < t.amount_cents <= active[t.account_id].txn_limit_cents for t in batch)
