-- SandboxBank ledger-db, schema v1 (decision log: SBX-LEDGER).
-- Idempotent: safe to run on every payments-api start.
-- Synthetic data only.

CREATE TABLE IF NOT EXISTS accounts (
    account_id   text PRIMARY KEY,
    customer_id  text NOT NULL,
    account_type text NOT NULL CHECK (account_type IN ('checking', 'savings', 'business')),
    currency     char(3) NOT NULL,
    status       text NOT NULL CHECK (status IN ('active', 'dormant', 'closed')),
    opened_on    date NOT NULL,
    txn_limit    numeric(14, 2) NOT NULL CHECK (txn_limit > 0)
);

CREATE TABLE IF NOT EXISTS merchants (
    merchant_id text PRIMARY KEY,
    name        text NOT NULL,
    mcc         char(4) NOT NULL,
    category    text NOT NULL,
    country     char(2) NOT NULL
);

-- Canary customers for RAI's privacy sentinel (FR-05.1): fake personal details,
-- never real people. Nothing downstream copies this table, so a value of it found
-- anywhere else is a leak. customer_id matches accounts.customer_id.
CREATE TABLE IF NOT EXISTS customers (
    customer_id   text PRIMARY KEY,
    full_name     text NOT NULL,
    email         text NOT NULL UNIQUE CHECK (email LIKE '%@example.invalid'),
    phone         text NOT NULL,
    card_number   char(16) NOT NULL UNIQUE,
    date_of_birth date NOT NULL,
    marker        text NOT NULL UNIQUE
);

-- One row per posted transaction. seq is the commit order RAI uses to measure RPO
-- ("last committed ledger record", FR-08.2); committed_at is the database clock.
CREATE TABLE IF NOT EXISTS ledger_transactions (
    seq            bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    txn_id         uuid PRIMARY KEY,
    batch_id       text NOT NULL,
    account_id     text NOT NULL REFERENCES accounts (account_id),
    merchant_id    text REFERENCES merchants (merchant_id),
    channel        text NOT NULL CHECK (channel IN ('card_present', 'ecommerce', 'ach', 'wire', 'p2p')),
    direction      text NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount         numeric(14, 2) NOT NULL CHECK (amount > 0),
    currency       char(3) NOT NULL,
    status         text NOT NULL CHECK (status IN ('posted', 'pending', 'declined')),
    created_at     timestamptz NOT NULL,
    is_fraud       boolean NOT NULL,
    request_sha256 char(64) NOT NULL,
    committed_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS ledger_transactions_account_idx ON ledger_transactions (account_id);
CREATE INDEX IF NOT EXISTS ledger_transactions_batch_idx ON ledger_transactions (batch_id);

-- The ledger is append-only: corrections are new rows, never edits.
CREATE OR REPLACE FUNCTION ledger_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger_transactions is append-only (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS ledger_append_only ON ledger_transactions;
CREATE TRIGGER ledger_append_only
    BEFORE UPDATE OR DELETE ON ledger_transactions
    FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
