-- 033_bedrock_reconciliation.sql
-- Daily 1:1 reconciliation of Bedrock token usage: what WE billed
-- (usage_events, provider='bedrock') vs. what AWS says it served
-- (CloudWatch AWS/Bedrock InputTokenCount/OutputTokenCount per ModelId).
--
-- Why this exists
-- ---------------
-- Bedrock traffic is pay-per-token against Rafael's own AWS account. The
-- business requirement is explicit: every token billed to a user must be
-- provably the token paid to AWS — consistent, verifiable, no drift.
-- usage_events rows are OUR ledger; this table stores the daily comparison
-- against AWS's ledger so drift becomes a queryable fact (and an alert),
-- not an end-of-month invoice surprise.
--
-- One row per (day, bedrock model id, region). Re-running a reconciliation
-- for the same key upserts (numbers may be refreshed while a day is still
-- accumulating; the nightly run reconciles the CLOSED previous day).
--
-- status values:
--   ok              — diff within tolerance
--   drift           — diff beyond tolerance → investigate (alert logged)
--   aws_unavailable — CloudWatch query failed; bridge-side numbers stored,
--                     AWS columns NULL. NOT silently ok — visibly unverified.

CREATE TABLE IF NOT EXISTS bedrock_reconciliation (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    day                   DATE NOT NULL,
    bedrock_model_id      TEXT NOT NULL,
    region                TEXT NOT NULL,
    -- Our ledger (usage_events, provider='bedrock', status=success)
    bridge_calls          BIGINT NOT NULL DEFAULT 0,
    bridge_input_tokens   BIGINT NOT NULL DEFAULT 0,
    bridge_output_tokens  BIGINT NOT NULL DEFAULT 0,
    -- AWS's ledger (CloudWatch AWS/Bedrock daily sums)
    aws_input_tokens      BIGINT,
    aws_output_tokens     BIGINT,
    -- Relative difference (aws - bridge) / aws, NULL when aws side missing
    input_diff_pct        DOUBLE PRECISION,
    output_diff_pct       DOUBLE PRECISION,
    status                TEXT NOT NULL,
    detail                TEXT,
    checked_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT bedrock_reconciliation_status_chk
        CHECK (status IN ('ok', 'drift', 'aws_unavailable')),
    CONSTRAINT bedrock_reconciliation_day_model_region_uq
        UNIQUE (day, bedrock_model_id, region)
);

CREATE INDEX IF NOT EXISTS idx_bedrock_reconciliation_day
    ON bedrock_reconciliation (day DESC);

CREATE INDEX IF NOT EXISTS idx_bedrock_reconciliation_status
    ON bedrock_reconciliation (status) WHERE status <> 'ok';
