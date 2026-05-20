-- Add role column to users table.
-- Mirrors the 6-value UserRoleSchema from packages/api-validation/src/common-schemas.ts
-- so consuming apps receive the Bridge user's actual role rather than a hardcoded default.
-- Existing users receive 'user' via the column DEFAULT — safe for zero-downtime migration.

ALTER TABLE users ADD COLUMN IF NOT EXISTS
    role VARCHAR(64) NOT NULL DEFAULT 'user';

-- Idempotent constraint: drop first, then re-add.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('super_admin', 'tenant_admin', 'admin', 'owner', 'member', 'user'));
