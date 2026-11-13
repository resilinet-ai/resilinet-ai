-- SandboxBank analytics-db, warehouse schema v1 (decision log: SBX-ETL).
-- Idempotent: safe to run on every etl-job start. Synthetic data only.
--
-- Like many real warehouses, natural keys here are NOT enforced as unique.
-- That is deliberate: the defect catalogue (for example D12, join fan-out from a
-- duplicated dimension key) must be able to happen so RAI can be scored on
-- detecting it. Integrity is the ETL's job and RAI's to verify.

CREATE SCHEMA IF NOT EXISTS warehouse;

CREATE TABLE IF NOT EXISTS warehouse.dim_account (
    account_sk   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   text NOT NULL,
    customer_id  text NOT NULL,
    account_type text NOT NULL,
    currency     char(3) NOT NULL,
    status       text NOT NULL,
    opened_on    date NOT NULL,
    txn_limit    numeric(14, 2) NOT NULL,
    loaded_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS dim_account_natural_idx ON warehouse.dim_account (account_id);

CREATE TABLE IF NOT EXISTS warehouse.dim_merchant (
    merchant_sk bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    merchant_id text NOT NULL,
    name        text NOT NULL,
    mcc         char(4) NOT NULL,
    category    text NOT NULL,
    country     char(2) NOT NULL,
    loaded_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS dim_merchant_natural_idx ON warehouse.dim_merchant (merchant_id);

-- Posted ledger transactions, one row each, partitioned logically by business_date.
CREATE TABLE IF NOT EXISTS warehouse.fact_transaction (
    business_date date NOT NULL,
    ledger_seq    bigint NOT NULL,
    txn_id        uuid NOT NULL,
    account_id    text NOT NULL,
    merchant_id   text,
    channel       text NOT NULL,
    direction     text NOT NULL,
    amount        numeric(14, 2) NOT NULL,
    currency      char(3) NOT NULL,
    created_at    timestamptz NOT NULL,
    is_fraud      boolean NOT NULL,
    committed_at  timestamptz NOT NULL,
    loaded_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS fact_transaction_date_idx ON warehouse.fact_transaction (business_date);
CREATE INDEX IF NOT EXISTS fact_transaction_txn_idx ON warehouse.fact_transaction (txn_id);

CREATE TABLE IF NOT EXISTS warehouse.settlement_account_daily (
    business_date date NOT NULL,
    account_id    text NOT NULL,
    account_type  text NOT NULL,
    currency      char(3) NOT NULL,
    debit_total   numeric(16, 2) NOT NULL,
    credit_total  numeric(16, 2) NOT NULL,
    net_amount    numeric(16, 2) NOT NULL,
    txn_count     integer NOT NULL,
    settled_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS settlement_account_date_idx
    ON warehouse.settlement_account_daily (business_date);

CREATE TABLE IF NOT EXISTS warehouse.settlement_merchant_daily (
    business_date date NOT NULL,
    merchant_id   text NOT NULL,
    category      text NOT NULL,
    gross_amount  numeric(16, 2) NOT NULL,
    txn_count     integer NOT NULL,
    settled_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS settlement_merchant_date_idx
    ON warehouse.settlement_merchant_daily (business_date);

-- Restart safety: the watermark moves in the same transaction as each loaded chunk,
-- and every date a chunk touches is queued here until its settlement is rebuilt.
CREATE TABLE IF NOT EXISTS warehouse.etl_watermark (
    pipeline   text PRIMARY KEY,
    ledger_seq bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS warehouse.settlement_pending (
    business_date date PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS warehouse.etl_runs (
    run_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    from_seq      bigint NOT NULL,
    to_seq        bigint NOT NULL,
    rows_read     integer NOT NULL,
    rows_loaded   integer NOT NULL,
    amount_loaded numeric(16, 2) NOT NULL,
    dates_settled integer NOT NULL
);
