-- Migration 020 — plans table: pricing as data, not code.
--
-- Replaces the hardcoded PLANS dict in src/budget/plans.py + its TS mirror
-- in werkingflow-production/packages/usage-billing-admin/src/server/PlanManager.ts.
--
-- The plan IDs stay as Postgres enums (typed FKs from subscriptions /
-- app_licenses), but their attributes (price, description, name, …) move
-- to this table where they can be iterated on without a Bridge code-deploy.
--
-- Migration steps for the codebase (separate commits):
--   1. This migration: table + seed of current plans.
--   2. Bridge plans.py: replace static PLANS dict with DB read at startup,
--      plus a POST /v1/billing/plans/reload endpoint for hot-swap.
--   3. Frontend PlanManager.ts: delete, replace any consumers with calls
--      to the existing /v1/billing/plans endpoint.

CREATE TABLE IF NOT EXISTS plans (
    id              plan_id       PRIMARY KEY,
    app_id          app_id        NOT NULL,
    name            VARCHAR(64)   NOT NULL,
    price_eur       NUMERIC(10,2) NOT NULL CHECK (price_eur >= 0),
    interval        VARCHAR(16)   NOT NULL CHECK (interval IN ('month','project','once')),
    api_budget_eur  NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (api_budget_eur >= 0),
    description     TEXT          NOT NULL,
    is_trial        BOOLEAN       NOT NULL DEFAULT FALSE,
    -- is_active toggles a plan in/out of the public catalog without deleting
    -- the row (FK-safe — existing subscriptions on a deactivated plan keep
    -- working until they expire). Newly seeded rows default to active.
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,
    sort_order      INTEGER       NOT NULL DEFAULT 0,
    -- metadata: feature flags / badges / experimental copy A/B variants etc.
    -- Example today: {"refundGuarantee": {"days": 14, "until": "pdf-export"}}
    -- for energy-project. Frontend gates UI rendering on these keys instead
    -- of hardcoding lists like PLANS_WITH_REFUND_GUARANTEE.
    metadata        JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plans_app_id    ON plans(app_id);
CREATE INDEX IF NOT EXISTS idx_plans_is_active ON plans(is_active);

-- Seed: mirrors src/budget/plans.py state at time of migration (2026-05-26).
-- WerkING Safety is paused — its plan_id 'safety-project' exists in the enum
-- (for historical subscriptions) but is intentionally NOT seeded here.
-- Re-activation: add a new migration with the seed row.
--
-- ON CONFLICT (id) DO NOTHING — guards against a manual re-run outside the
-- migration runner; the runner itself already enforces single-application
-- via schema_migrations checksum tracking.
INSERT INTO plans (id, app_id, name, price_eur, interval, api_budget_eur, description, is_trial, sort_order, metadata) VALUES
    ('trial',           'werking-report', '7-Tage-Test',      0,    'month',    5,  '7 Tage kostenlos, voller Zugang. Danach EUR 250 oder weg.',                       TRUE,  10, '{}'::jsonb),
    ('report-standard', 'werking-report', 'Standard',         250,  'month',    50, 'Voller Funktionsumfang. KI-Budget inklusive, weitere Sitze zum gleichen Preis.',  FALSE, 20, '{}'::jsonb),
    ('energy-project',  'werking-energy', 'Energy-Projekt',   1000, 'project',  100,'KI-Budget inklusive. Beliebig viele Neuberechnungen, solange das Budget reicht.', FALSE, 30, '{"refundGuarantee": {"days": 14, "until": "pdf-export"}}'::jsonb),
    ('noise-tbd',       'werking-noise',  'WerkING Noise',    0,    'month',    0,  'Akustik-Gutachten mit KI-Unterstützung. Aktuell in Beta-Tests.',                  FALSE, 40, '{}'::jsonb),
    ('engelmann-custom','engelmann',      'Engelmann Custom', 0,    'month',    0,  'Custom-Projekt, kein WerkING-Produkt. Separate Konditionen.',                     FALSE, 90, '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;
