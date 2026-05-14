-- Phase X1 — Sandbox billing_mode + lease tables
--
-- Three changes in one migration:
--   1. billing_mode column on tenants (default 'subscription' for Dev-Phase)
--   2. sandbox_leases — active OAuth lease per sandbox session
--   3. sandbox_usage_events — shadow usage tracking (no deduct for subscription)

-- 1. billing_mode on tenants
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS billing_mode TEXT NOT NULL DEFAULT 'subscription';

-- Backfill: legacy tenants that already have Mollie subscriptions → 'subscription'.
-- All others stay at 'subscription' default (Dev-Phase: every new tenant starts here).
-- Admin can override via PATCH /v1/identity/tenants/:id.
CREATE INDEX IF NOT EXISTS idx_tenants_billing_mode ON tenants(billing_mode);

-- 2. sandbox_leases
CREATE TABLE IF NOT EXISTS sandbox_leases (
    lease_id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID        NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    tenant_id           VARCHAR(128) NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    app                 TEXT        NOT NULL,
    account_id          TEXT        NOT NULL,   -- "engelmann"/"office"/"gmail"/"werking"
    session_id          TEXT,                   -- filled by attach-session after container start
    leased_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL,
    released_at         TIMESTAMPTZ,            -- NULL while active
    last_heartbeat_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_active_leases
    ON sandbox_leases(account_id)
    WHERE released_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_leases_user
    ON sandbox_leases(user_id, leased_at DESC);

-- 3. sandbox_usage_events
CREATE TABLE IF NOT EXISTS sandbox_usage_events (
    id                      BIGSERIAL   PRIMARY KEY,
    litellm_call_id         TEXT        UNIQUE NOT NULL,   -- idempotency key
    user_id                 UUID        NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    tenant_id               VARCHAR(128) NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    session_id              TEXT        NOT NULL,
    lease_id                UUID        REFERENCES sandbox_leases(lease_id) ON DELETE SET NULL,
    account_id              TEXT        NOT NULL,
    app                     TEXT        NOT NULL,
    model                   TEXT        NOT NULL,
    input_tokens            INT         NOT NULL,
    output_tokens           INT         NOT NULL,
    cache_read_tokens       INT         NOT NULL DEFAULT 0,
    cache_creation_tokens   INT         NOT NULL DEFAULT 0,
    hypothetical_cost_eur   NUMERIC(10,6) NOT NULL,
    real_cost_eur           NUMERIC(10,6) NOT NULL DEFAULT 0,
    billing_mode            TEXT        NOT NULL,
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usage_user_recorded
    ON sandbox_usage_events(user_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_usage_tenant_recorded
    ON sandbox_usage_events(tenant_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_usage_session
    ON sandbox_usage_events(session_id);

CREATE INDEX IF NOT EXISTS idx_usage_app_model
    ON sandbox_usage_events(app, model);
