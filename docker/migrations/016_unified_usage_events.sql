-- Migration 016 — Unified usage_events ledger + per-user provider_config
--
-- Problem solved:
--   Sandbox usage (structured, with tokens+cost) lived in sandbox_usage_events.
--   Workflow usage lived only in activities.payload JSONB — no dedicated columns,
--   not queryable by token/cost without JSON extraction.
--   The Platform Admin could only see activities → sandbox was invisible; workflow
--   costs were estimated in the UI rather than read from a proper record.
--
-- Solution:
--   A single `usage_events` table covering workflow + sandbox + chat sources.
--   `sandbox_usage_events` is replaced by a read-compatible VIEW so existing
--   read-only queries (by-user, by-tenant, by-session) keep working without changes.
--   All new writes go to `usage_events` directly.
--
--   Mandatory ledger fields per event:
--     source, user_id, tenant_id, app, model, provider, billing_mode,
--     input/output tokens, real_cost_eur, hypothetical_cost_eur, pricing_version
--
--   Source-specific extras live in provider_metadata JSONB:
--     sandbox: lease_id, account_id, litellm_call_id
--     workflow: agent_id, workflow_id, feature
--
-- Also adds users.provider_config JSONB for the coming per-user Bedrock switch.
-- Only schema + read-path here — routing/enforcement is a separate later sub.
--
-- This migration MUST NOT be executed against a live database directly.
-- Deploy only via the reviewed migration pipeline.

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. New enum types
-- ─────────────────────────────────────────────────────────────────────────────

DO $$ BEGIN
    CREATE TYPE usage_source AS ENUM ('workflow', 'sandbox', 'chat');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- billing_mode semantics:
--   pay_per_token       — real_cost_eur is the actual charged amount
--   flat_rate_estimated — user is on a subscription; real_cost_eur = 0,
--                         hypothetical_cost_eur shows what it would have cost
DO $$ BEGIN
    CREATE TYPE billing_mode_enum AS ENUM ('pay_per_token', 'flat_rate_estimated');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. usage_events — the single queryable usage ledger
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS usage_events (
    id                    BIGSERIAL         PRIMARY KEY,
    recorded_at           TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    source                usage_source      NOT NULL,

    -- Attribution
    user_id               UUID              NOT NULL REFERENCES users(id)    ON DELETE RESTRICT,
    tenant_id             VARCHAR(128)      NOT NULL REFERENCES tenants(id)  ON DELETE RESTRICT,
    app                   TEXT,                          -- NULL for internal/non-app calls
    app_env               app_env,                       -- prod|staging|local from X-App-Env

    -- LLM dimensions
    model                 TEXT              NOT NULL,
    provider              TEXT              NOT NULL DEFAULT 'anthropic',
    region                TEXT,                          -- NULL = shared endpoint

    -- Token counters
    input_tokens          INT               NOT NULL DEFAULT 0,
    output_tokens         INT               NOT NULL DEFAULT 0,
    cache_read_tokens     INT               NOT NULL DEFAULT 0,
    cache_creation_tokens INT               NOT NULL DEFAULT 0,

    -- Cost (both always present; for flat_rate_estimated real_cost_eur = 0)
    billing_mode          billing_mode_enum NOT NULL,
    real_cost_eur         NUMERIC(10,6)     NOT NULL DEFAULT 0,
    hypothetical_cost_eur NUMERIC(10,6)     NOT NULL,
    pricing_version       TEXT              NOT NULL DEFAULT 'v1',

    -- Session (nullable — populated for sandbox, NULL for workflow/chat)
    session_id            TEXT,

    -- Source-specific metadata (lease_id, account_id for sandbox;
    -- agent_id, workflow_id for workflow)
    provider_metadata     JSONB             NOT NULL DEFAULT '{}',

    -- Dedup key — litellm_call_id for sandbox, bridge-generated UUID for workflow.
    -- NULL is allowed for back-compat with pre-migration workflow rows that had none.
    idempotency_key       TEXT              UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_usage_events_user_recorded
    ON usage_events(user_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_recorded
    ON usage_events(tenant_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_usage_events_source
    ON usage_events(source);

CREATE INDEX IF NOT EXISTS idx_usage_events_app
    ON usage_events(app);

CREATE INDEX IF NOT EXISTS idx_usage_events_model
    ON usage_events(model);

-- Session-scoped aggregates (sandbox usage by session)
CREATE INDEX IF NOT EXISTS idx_usage_events_session
    ON usage_events(session_id)
    WHERE session_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Migrate existing sandbox_usage_events → usage_events
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO usage_events (
    recorded_at,
    source,
    user_id,
    tenant_id,
    app,
    model,
    provider,
    region,
    input_tokens,
    output_tokens,
    cache_read_tokens,
    cache_creation_tokens,
    billing_mode,
    real_cost_eur,
    hypothetical_cost_eur,
    pricing_version,
    session_id,
    idempotency_key,
    provider_metadata
)
SELECT
    recorded_at,
    'sandbox'::usage_source,
    user_id,
    tenant_id,
    app,
    model,
    'anthropic',
    NULL,
    input_tokens,
    output_tokens,
    cache_read_tokens,
    cache_creation_tokens,
    -- Map legacy TEXT billing_mode to the new enum:
    -- 'subscription' → flat_rate_estimated (Bridge pays flat; per-call charge = 0)
    -- anything else  → pay_per_token
    CASE
        WHEN billing_mode = 'pay_per_token' THEN 'pay_per_token'::billing_mode_enum
        ELSE 'flat_rate_estimated'::billing_mode_enum
    END,
    real_cost_eur,
    hypothetical_cost_eur,
    'v1',
    session_id,
    litellm_call_id,
    jsonb_build_object(
        'litellm_call_id', litellm_call_id,
        'lease_id',        lease_id::text,
        'account_id',      account_id
    )
FROM sandbox_usage_events
ON CONFLICT (idempotency_key) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Replace sandbox_usage_events table with a read-compatible VIEW
--
-- Existing read queries (by-user, by-tenant, by-session, model breakdown)
-- continue to work without changes because the VIEW exposes the same column
-- names. All future INSERTs must go to usage_events directly.
-- ─────────────────────────────────────────────────────────────────────────────

DROP TABLE IF EXISTS sandbox_usage_events CASCADE;

CREATE OR REPLACE VIEW sandbox_usage_events AS
SELECT
    idempotency_key                             AS litellm_call_id,
    user_id,
    tenant_id,
    session_id,
    (provider_metadata->>'lease_id')::UUID      AS lease_id,
    (provider_metadata->>'account_id')::TEXT    AS account_id,
    app,
    model,
    input_tokens,
    output_tokens,
    cache_read_tokens,
    cache_creation_tokens,
    hypothetical_cost_eur,
    real_cost_eur,
    -- Expose legacy TEXT billing_mode for callers that compare to 'subscription'
    CASE billing_mode
        WHEN 'pay_per_token' THEN 'pay_per_token'
        ELSE 'subscription'
    END                                         AS billing_mode,
    recorded_at
FROM usage_events
WHERE source = 'sandbox';

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Per-user provider override (Bedrock-switch preparation)
--
-- NULL = inherit tenant default (tenants.billing_mode + shared OAuth endpoint)
-- Non-null JSON shape (validated by the application layer, not a DB constraint):
--   { "provider": "bedrock", "region": "eu-central-1",
--     "model": "<bedrock-model-id>", "billing_mode": "pay_per_token",
--     "budget_limit_eur": 100.0 }
--
-- Routing and budget-enforcement are NOT added here — those are a separate sub.
-- This migration only adds the column and ensures it is readable via the API.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS provider_config JSONB;
