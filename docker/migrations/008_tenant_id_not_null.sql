-- Phase 8 — tenant_id NOT NULL on tenant-scoped tables.
--
-- Closes the contract: every activity-log and feedback row MUST carry a
-- tenant_id. Combined with the resolve_tenant_id() helper at the API layer,
-- silent tenant-anonymous inserts are no longer possible.
--
-- Historical anonymous rows (no tenant_id AND no actor_user_id AND no
-- recoverable hint in payload) are deleted in the same transaction —
-- they cannot be retroactively classified, and keeping them would block
-- the constraint forever. Rafael authorised this cleanup on 2026-05-14
-- with "aktuell arbeitet noch niemand in production".
--
-- See ADR 0007 — tenant from auth context.
--
-- Forward-only / idempotent: ALTER ... SET NOT NULL stays a no-op once
-- applied, and the DELETE matches an ever-shrinking set (will be 0 after
-- the contract is in effect because the API rejects NULL inserts).

-- 1) Cleanup unrecoverable rows. Match the most conservative possible set:
--    only delete rows that have NO way to determine the tenant.
DELETE FROM activities
WHERE tenant_id IS NULL
  AND actor_user_id IS NULL
  AND (payload->>'tenantId') IS NULL
  AND (payload->>'userId')   IS NULL;

DELETE FROM feedback
WHERE tenant_id IS NULL
  AND user_id IS NULL;

-- 2) Pre-flight: any remaining NULLs mean partial recoverability — operator
--    must reconcile them by hand (rare; the cleanup above is the common case).
DO $$
DECLARE
    bad_activities INTEGER;
    bad_feedback   INTEGER;
BEGIN
    SELECT COUNT(*) INTO bad_activities FROM activities WHERE tenant_id IS NULL;
    SELECT COUNT(*) INTO bad_feedback   FROM feedback   WHERE tenant_id IS NULL;
    IF bad_activities > 0 OR bad_feedback > 0 THEN
        RAISE EXCEPTION
            'Migration 008 aborted: % activities and % feedback rows still have NULL tenant_id '
            'after cleanup (rows that have actor/user but no tenant). '
            'Reconcile manually before re-running.',
            bad_activities, bad_feedback;
    END IF;
END $$;

-- 3) Enforce the contract at the storage layer.
ALTER TABLE activities ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE feedback   ALTER COLUMN tenant_id SET NOT NULL;
