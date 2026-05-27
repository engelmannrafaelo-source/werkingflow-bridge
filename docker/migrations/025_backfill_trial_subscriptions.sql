-- 025_backfill_trial_subscriptions.sql
--
-- Backfill: any user that has a trial app_license but no corresponding
-- 'trial' subscription row must get one — otherwise the customer portal's
-- forced-trial gate (SubscriptionSection.tsx::isTrial) treats them as a
-- pre-trial guest and blocks the Standard-purchase CTA.
--
-- Self-service registration before commit pair 024+register-trial-row
-- created the app_license but not the subscription. This repairs them.
--
-- Safety:
--   * Idempotent — re-running is a no-op (the WHERE NOT EXISTS guard).
--   * Only fills missing rows; never modifies existing subscriptions.
--   * Affects only users whose app_license has plan_id='trial' (the
--     register-default). Engelmann-custom users and others are untouched.
--   * The new rows have status='active' and mollie_customer_id=NULL,
--     which is allowed by migration 024's CHECK constraint.

INSERT INTO subscriptions
    (user_id, app_id, plan_id, status, mollie_customer_id, seats, started_at)
SELECT
    al.user_id,
    al.app_id,
    'trial'::plan_id,
    'active'::subscription_status,
    NULL,
    1,
    NOW()
FROM app_licenses al
WHERE al.plan_id = 'trial'
  AND NOT EXISTS (
    SELECT 1
    FROM subscriptions s
    WHERE s.user_id = al.user_id
      AND s.app_id = al.app_id
      AND s.plan_id = 'trial'
  );
