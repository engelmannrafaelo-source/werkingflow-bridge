-- 027_subscription_trial_warning_sent_at.sql
--
-- Adds idempotency markers for trial-expiry warning emails. We send two
-- warnings per trial: 3 days before trial_ends_at, and 1 day before. Each
-- send is gated by a NULL check on its respective column — once stamped,
-- the warning is never re-sent, even if the cron loop fires repeatedly.
--
-- Why two columns instead of one with a "stage" enum? Two reasons:
--   1. Idempotency reads are a NULL check, no parsing or comparison.
--   2. We can audit "did the 3-day warning go out for user X" in isolation
--      from the 1-day warning, which matters for support post-mortems.
--
-- Both columns are NULL for non-trial subscriptions (covered by the existing
-- trial_ends_at CHECK constraint — non-trial subs have no trial state).
-- We do NOT add a CHECK constraint here: the cron job is the single writer,
-- and a missing send (column stays NULL) is a recoverable state, not a
-- contract violation.

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS trial_warning_3d_sent_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS trial_warning_1d_sent_at TIMESTAMPTZ;

-- Index for the cron scan: find active trials with trial_ends_at in the
-- warning window AND warning not yet sent. Partial index keeps it tiny
-- since trial subscriptions are a small fraction of total subs and the
-- warning windows are 2 days out of a 7-day trial.
CREATE INDEX IF NOT EXISTS idx_subscriptions_trial_warning_3d_due
    ON subscriptions(trial_ends_at)
    WHERE plan_id = 'trial'
      AND status = 'active'
      AND trial_warning_3d_sent_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_subscriptions_trial_warning_1d_due
    ON subscriptions(trial_ends_at)
    WHERE plan_id = 'trial'
      AND status = 'active'
      AND trial_warning_1d_sent_at IS NULL;
