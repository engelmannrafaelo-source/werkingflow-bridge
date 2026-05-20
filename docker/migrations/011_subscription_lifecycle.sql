-- Phase 4 — Subscription lifecycle columns.
-- suspended_at: set when a recurring payment fails (subscription.suspended event).
-- expired_at:   set when a trial subscription's period ends (trial_period_ended event).
--
-- Both are NULL for active/cancelled subscriptions and filled exactly once
-- on the first state transition. The existing subscription_status enum already
-- includes 'suspended' and 'expired'; these columns capture the timestamp.

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS expired_at   TIMESTAMPTZ;
