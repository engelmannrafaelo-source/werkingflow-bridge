-- 026_subscription_trial_ends_at.sql
--
-- Adds the trial-period end timestamp to subscriptions. Forced-trial funnel
-- (Rafael's design): user registers → trial subscription is active for
-- N days → after that the customer portal blocks new actions until the
-- user converts to a paid Standard plan.
--
-- Why a column and not just relying on `started_at + interval '7 days'`?
-- Two reasons:
--   1. The trial length is policy, not contract — making it a column lets
--      us extend a specific customer's trial (Sales/Support gesture) by
--      a single UPDATE, without touching the global default.
--   2. The lazy-expire query in list_subscriptions reads this column
--      directly; a derived calculation would couple the expire-logic to
--      the default-trial-length constant in two places (register handler
--      and list_subscriptions). One column, one truth.
--
-- Backfill: existing trial subscriptions (the rows created by migration
-- 025 + today's QA registrations) get started_at + 7 days. The 7-day
-- default matches the registration-stage description in
-- packages/api-validation/required-fields.yaml.
--
-- Safety: column is NULL-allowed for non-trial subscriptions. A CHECK
-- constraint enforces: trial subs MUST have trial_ends_at; non-trial subs
-- MUST NOT. Fail-loud on contract violation.

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ;

-- Backfill existing trial rows.
UPDATE subscriptions
SET trial_ends_at = started_at + INTERVAL '7 days'
WHERE plan_id = 'trial'
  AND trial_ends_at IS NULL;

-- Invariant: trial subscriptions carry trial_ends_at; non-trial don't.
-- Fail-loud on writes that violate the contract.
ALTER TABLE subscriptions
    ADD CONSTRAINT subscriptions_trial_ends_at_ck
    CHECK (
        (plan_id = 'trial' AND trial_ends_at IS NOT NULL)
        OR
        (plan_id <> 'trial' AND trial_ends_at IS NULL)
    );

-- Index for the lazy-expire scan (list_subscriptions): find active trials
-- past their end date. Partial index — only the small minority of rows
-- where plan_id='trial' are indexed.
CREATE INDEX IF NOT EXISTS idx_subscriptions_active_trials_expiry
    ON subscriptions(trial_ends_at)
    WHERE plan_id = 'trial' AND status = 'active';
