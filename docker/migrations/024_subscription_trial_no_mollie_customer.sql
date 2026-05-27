-- 024_subscription_trial_no_mollie_customer.sql
--
-- Forced-trial funnel: /v1/auth/register seeds a subscription with
-- plan_id='trial', status='active' so the customer portal recognises
-- the user as a trial customer (frontend gate `isTrial` reads from the
-- subscriptions table). Before this migration, mollie_customer_id was
-- NOT NULL — but a freshly registered user has no Mollie customer yet
-- (Mollie customer creation is deferred to the first paid checkout via
-- /v1/billing/subscription/checkout).
--
-- Loosening the NOT NULL is the minimal change. A CHECK constraint enforces
-- the invariant: only trial subscriptions may have NULL mollie_customer_id.
-- Any non-trial subscription (paid or transitioning) MUST have one.

ALTER TABLE subscriptions
    ALTER COLUMN mollie_customer_id DROP NOT NULL;

-- Tightening guard: only the trial plan may skip mollie_customer_id.
ALTER TABLE subscriptions
    ADD CONSTRAINT subscriptions_trial_no_mollie_customer_ck
    CHECK (mollie_customer_id IS NOT NULL OR plan_id = 'trial');
