-- 023_self_registered_users_to_owner.sql
--
-- Backfill: every user who is the registered owner of their personal tenant
-- (i.e. tenants.owner_user_id = users.id) must have role='owner'. Before this
-- migration, self-service registration via POST /v1/auth/register inserted
-- the user with role='user' (default in identity/routes.py:register) — and
-- the customer portal then refused to render the purchase UI because the
-- ADMIN_ROLES set in packages/usage-billing-admin SubscriptionSection.tsx
-- requires role ∈ {super_admin, tenant_admin, admin, owner}. End result:
-- newly registered solo customers landed in the "ask your administrator"
-- dead-end, unable to buy any plan via Mollie self-service.
--
-- The register-handler code path is now fixed (commit pairs with this
-- migration) to insert role='owner' directly. This migration repairs the
-- existing population so the historical self-registered users get the same
-- treatment.
--
-- Safety:
--   * Idempotent — re-running is a no-op.
--   * Affects only users that already are the owner_user_id of their tenant.
--     Users who are non-owner members of a multi-user tenant are NOT touched
--     (their role stays 'user' or whatever was set explicitly).
--   * Does NOT downgrade anyone. Only role='user' rows get upgraded.

UPDATE users
SET role = 'owner',
    updated_at = NOW()
WHERE role = 'user'
  AND id IN (
    SELECT owner_user_id
    FROM tenants
    WHERE owner_user_id IS NOT NULL
  );
