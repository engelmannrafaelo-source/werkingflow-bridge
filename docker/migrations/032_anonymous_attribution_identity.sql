-- 032_anonymous_attribution_identity.sql
-- Dedicated accounting identity for EXPLICITLY anonymous user-facing calls
-- (fail-closed attribution contract, Rafael decision B 2026-07-02).
--
-- Why this exists
-- ---------------
-- Every user-facing Bridge call must carry X-User-ID — either a real userId
-- or an explicit marker 'anonymous:<grund>' (e.g. the werking-report public
-- check funnel, which by design has no logged-in user). Anonymous calls must
-- land in the accounting (activities + usage_events) as their OWN bucket:
-- separate from real users AND separate from "attribution missing" (which is
-- only counted in metrics and — once BRIDGE_ATTRIBUTION_ENFORCE=true — rejected).
--
-- usage_events.user_id / tenant_id are NOT NULL with FKs to users/tenants —
-- deliberately so (schema invariant: every booked row has an identity). Rather
-- than weakening that invariant with nullable columns, anonymous calls book to
-- this dedicated synthetic identity. All existing usage/billing queries then
-- show the anonymous bucket for free, cleanly separated by tenant/user.
--
-- Properties of the identity:
--   * tenant 'bridge-anonymous', account_type='internal' → excluded from
--     customer-facing tenant views the same way other internal tenants are.
--   * billing_mode stays the 'subscription' default → usage_events rows book
--     as flat_rate_estimated with real_cost_eur=0: anonymous traffic is
--     tracked (hypothetical cost), never real-billed. Budget deduction is
--     additionally skipped in code (ai_call_writer).
--   * user has NO password_hash → can never log in; email_verified=TRUE so it
--     can never surface in unverified-account sweeps. Fixed UUID so all
--     environments book anonymous usage to the same recognizable id.
--
-- Idempotent + forward-only (runner provides the transaction).

INSERT INTO tenants (id, name, account_type, billing_mode)
VALUES (
    'bridge-anonymous',
    'Anonymous (explicit anonymous:<grund> attribution)',
    'internal',
    'subscription'
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO users (id, email, name, tenant_id, password_hash, role, email_verified)
VALUES (
    '00000000-0000-4000-a000-000000000001',
    'anonymous@bridge.internal',
    'Anonymous Attribution Bucket',
    'bridge-anonymous',
    NULL,
    'user',
    TRUE
)
ON CONFLICT (email) DO NOTHING;
