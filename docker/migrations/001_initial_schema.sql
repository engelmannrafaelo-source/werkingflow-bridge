-- Bridge Identity + Billing + Activity Schema
-- Generated from packages/usage-billing-admin/src/types/
-- (identity.ts, budget.ts, billing.ts, activity.ts)

CREATE TYPE app_id AS ENUM (
    'werking-report', 'werking-energy', 'werking-safety', 'werking-noise', 'engelmann'
);

CREATE TYPE plan_id AS ENUM (
    'trial', 'report-standard', 'energy-project', 'safety-project', 'noise-tbd', 'engelmann-custom'
);

CREATE TYPE subscription_status AS ENUM (
    'pending', 'active', 'cancelled', 'suspended', 'expired'
);

CREATE TYPE activity_category AS ENUM (
    'auth', 'user', 'tenant', 'billing', 'workflow', 'admin', 'storage', 'security', 'system'
);

CREATE TYPE pending_payment_type AS ENUM (
    'subscription_first', 'topup'
);

-- Identity: tenants first (users FK back)
CREATE TABLE tenants (
    id            VARCHAR(128) PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    owner_user_id UUID,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE users (
    id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    email      VARCHAR(255) UNIQUE NOT NULL,
    name       VARCHAR(255) NOT NULL,
    tenant_id  VARCHAR(128) NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    password_hash TEXT,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE tenants
    ADD CONSTRAINT fk_tenants_owner_user
    FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE SET NULL;

CREATE TABLE app_licenses (
    id         UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    app_id     app_id  NOT NULL,
    plan_id    plan_id NOT NULL,
    start_date DATE    NOT NULL,
    end_date   DATE,
    seats      INTEGER CHECK (seats > 0),
    UNIQUE (user_id, app_id)
);

CREATE TABLE sessions (
    id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      VARCHAR(512) UNIQUE NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ  NOT NULL
);
CREATE INDEX idx_sessions_token      ON sessions(token);
CREATE INDEX idx_sessions_expires_at ON sessions(expires_at);

-- Budget
CREATE TABLE user_budgets (
    user_id         UUID        PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    monthly_budgets JSONB       NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE user_topup_balances (
    user_id     UUID          PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    balance_eur NUMERIC(10,4) NOT NULL DEFAULT 0 CHECK (balance_eur >= 0),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- Billing
CREATE TABLE mollie_customers (
    user_id            UUID         PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    mollie_customer_id VARCHAR(128) UNIQUE NOT NULL,
    email              VARCHAR(255) NOT NULL,
    name               VARCHAR(255) NOT NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE subscriptions (
    id                       UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  UUID                NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    app_id                   app_id              NOT NULL,
    plan_id                  plan_id             NOT NULL,
    status                   subscription_status NOT NULL DEFAULT 'pending',
    mollie_customer_id       VARCHAR(128)        NOT NULL,
    mollie_subscription_id   VARCHAR(128),
    -- Idempotency guard for _activate_subscription: Mollie may retry the webhook
    -- POST until it gets a 200. mollie_first_payment_id holds the pending-payment
    -- id that triggered activation, so duplicate webhook firings find the existing
    -- row instead of inserting a second 'active' subscription.
    mollie_first_payment_id  VARCHAR(128)        UNIQUE,
    seats                    INTEGER             NOT NULL DEFAULT 1 CHECK (seats > 0),
    started_at               TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    cancelled_at             TIMESTAMPTZ,
    metadata                 JSONB
);
CREATE INDEX idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX idx_subscriptions_status  ON subscriptions(status);

CREATE TABLE credit_purchases (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID         NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    pack_eur           NUMERIC(8,2) NOT NULL CHECK (pack_eur > 0),
    paid_at            TIMESTAMPTZ  NOT NULL,
    mollie_customer_id VARCHAR(128) NOT NULL,
    mollie_payment_id  VARCHAR(128) NOT NULL UNIQUE
);
CREATE INDEX idx_credit_purchases_user_id ON credit_purchases(user_id);

CREATE TABLE pending_payments (
    payment_id VARCHAR(128)         PRIMARY KEY,
    user_id    UUID                 NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type       pending_payment_type NOT NULL,
    plan_id    plan_id,
    amount_eur NUMERIC(8,2)         NOT NULL CHECK (amount_eur > 0),
    created_at TIMESTAMPTZ          NOT NULL DEFAULT NOW()
);

-- Activity Log
CREATE TABLE activities (
    id             UUID              PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp      TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    category       activity_category NOT NULL,
    event_type     VARCHAR(128)      NOT NULL,
    actor_user_id  UUID,
    target_user_id UUID,
    tenant_id      VARCHAR(128),
    app_id         app_id,
    ip             VARCHAR(64),
    user_agent     TEXT,
    payload        JSONB             NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_activities_timestamp     ON activities(timestamp DESC);
CREATE INDEX idx_activities_actor_user_id ON activities(actor_user_id);
CREATE INDEX idx_activities_tenant_id     ON activities(tenant_id);
CREATE INDEX idx_activities_event_type    ON activities(event_type);
