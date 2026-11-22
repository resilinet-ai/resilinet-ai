-- SandboxBank P3: the fraud feature pipeline's tables in analytics-db (schema features).
-- Idempotent: the feature-job runs it at every start.

CREATE SCHEMA IF NOT EXISTS features;

-- One row per posted transaction: the model's eight inputs as the warehouse knows them,
-- the label, and the score the model-api gave, with the model that gave it.
CREATE TABLE IF NOT EXISTS features.fraud_features (
    txn_id              uuid PRIMARY KEY,
    ledger_seq          bigint NOT NULL,
    business_date       date NOT NULL,
    amount              double precision NOT NULL,
    amount_to_limit     double precision,
    channel             text,
    credit              double precision NOT NULL CHECK (credit IN (0, 1)),
    status              text,
    account_type        text,
    has_merchant        double precision NOT NULL CHECK (has_merchant IN (0, 1)),
    merchant_category   text,
    is_fraud            boolean NOT NULL,
    score               double precision NOT NULL CHECK (score >= 0 AND score <= 1),
    model_recipe_sha256 char(64) NOT NULL,
    scored_at           timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS fraud_features_seq_idx ON features.fraud_features (ledger_seq);
CREATE INDEX IF NOT EXISTS fraud_features_date_idx ON features.fraud_features (business_date);

-- How far the pipeline has read warehouse.fact_transaction, by ledger_seq.
CREATE TABLE IF NOT EXISTS features.feature_watermark (
    pipeline   text PRIMARY KEY,
    ledger_seq bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS features.feature_runs (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    from_seq     bigint NOT NULL,
    to_seq       bigint NOT NULL,
    rows_read    bigint NOT NULL,
    rows_scored  bigint NOT NULL,
    model_recipe_sha256 char(64)
);
