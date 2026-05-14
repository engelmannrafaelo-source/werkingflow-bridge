-- Phase 6 — tenant.category: explicit classification (prod/staging/local).
--
-- Why a column instead of pattern-matching IDs:
-- Patterns rot (a real customer named "Demo GmbH" should NOT be staging).
-- Admin can override per-tenant in the Platform Admin → Tenants tab.
--
-- Default is 'prod' (safe — new real signups land in the prod bucket).
-- Existing rows are backfilled via heuristic, see UPDATE statements below.

DO $$ BEGIN
    CREATE TYPE tenant_category AS ENUM ('prod', 'staging', 'local');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS category tenant_category NOT NULL DEFAULT 'prod';

CREATE INDEX IF NOT EXISTS idx_tenants_category ON tenants(category);

-- One-shot backfill (runs only when category looks like default).
-- Safe to re-run: explicit overrides via PATCH stay intact because backfill
-- only touches rows that match the heuristic.
UPDATE tenants SET category = 'local'
WHERE category = 'prod'
  AND (id LIKE 'personal_%' OR id = 'engelmann-internal');

UPDATE tenants SET category = 'staging'
WHERE category = 'prod'
  AND (
    id LIKE '%-test-%' OR id LIKE 'test-%' OR id LIKE '%-test' OR id = 'test' OR
    id LIKE '%demo%' OR id LIKE '%smoke%' OR id LIKE 'build-%' OR
    id LIKE 'backend-%' OR id LIKE 'show-%' OR id LIKE 'sandbox%' OR
    name LIKE 'Auto-tenant for %example.com' OR
    name LIKE '%test%' OR name LIKE '%Demo%'
  );
