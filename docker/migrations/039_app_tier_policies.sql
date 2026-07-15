-- 039_app_tier_policies.sql
-- Bridge-owned per-(app, agent) routing/billing policy.
--
-- Why this exists
-- ---------------
-- Some app call-sites must run on a specific provider tier for operational
-- reasons the app should not know about — concretely, werking-energy's
-- large-input LLM calls (claims/schema/sensor-position generation) overflow the
-- Claude-Code worker context (SDK sub-process compaction truncates the input)
-- and must be served by the direct Anthropic Messages API tier
-- (claude-direct-notools) instead of the worker pool.
--
-- Previously that routing decision was hard-coded as a provider_tier on the
-- app's outbound request — a Bridge-internal concern leaking into a customer
-- app. This table moves it to the Bridge: the app sends only attribution
-- (X-App-ID, agent id, X-User-ID); src/routing/app_tier_policy.py maps
-- (app_id, agent_id, app_env) → {target_tier, billing_account}.
--
-- Columns
--   app_id          the X-App-ID this policy applies to (required).
--   agent_id        the agent/route (usage_events feature) it applies to, or
--                   NULL = every agent of the app. More specific rows win.
--   app_env         prod|staging|local it applies to, or NULL = every env.
--   target_tier     provider tier to force (e.g. 'claude-direct-notools'), or
--                   NULL = no tier override (billing_account-only policy).
--   billing_account free-form internal account label the call's COST is booked
--                   to instead of the customer budget. Attribution (user/app/
--                   agent) stays on the usage row; only the deduction target
--                   changes. NULL = charge the customer as normal.
--   enabled         soft on/off without deleting the row.
--
-- The policy layer is additionally gated by BRIDGE_APP_TIER_POLICY_ENABLED and
-- fails OPEN (a missing table / lookup error → normal routing, never a 5xx):
-- this is a cost/operational optimisation, not a compliance pin.
--
-- Idempotent + forward-only (runner provides the transaction).

CREATE TABLE IF NOT EXISTS app_tier_policies (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    app_id          text NOT NULL,
    agent_id        text,
    app_env         text,
    target_tier     text,
    billing_account text,
    enabled         boolean NOT NULL DEFAULT TRUE,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);

-- One row per (app, agent, env) scope — NULLs distinct so a NULL-agent catch-all
-- and specific-agent rows can coexist. COALESCE keys keep the uniqueness stable.
CREATE UNIQUE INDEX IF NOT EXISTS app_tier_policies_scope_uq
    ON app_tier_policies (
        app_id,
        COALESCE(agent_id, '*'),
        COALESCE(app_env, '*')
    );

-- Seed: the one route empirically shown to overflow the worker context.
-- werking-energy 'claims' generation → direct Anthropic Messages API, cost to
-- the internal account. schema-generierung / sensor-positions are NOT seeded —
-- add them only when they actually reproduce the compaction (avoid paying
-- per-token for calls the worker pool serves fine for free).
INSERT INTO app_tier_policies (app_id, agent_id, app_env, target_tier, billing_account, note)
VALUES (
    'werking-energy',
    'claims-generierung',
    NULL,
    'claude-direct-notools',
    'werking-internal',
    'Large-input claims generation overflows the worker context (SDK compaction). '
    'Route direct; bill internal. Seeded by 039.'
)
ON CONFLICT (app_id, COALESCE(agent_id, '*'), COALESCE(app_env, '*')) DO NOTHING;
