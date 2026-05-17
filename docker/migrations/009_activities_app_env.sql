-- Phase 9 — app_env: the environment a call actually came from.
--
-- Why this exists:
--   The Platform Admin "mode" filter (prod/staging/local) used to read
--   tenant.category — a hand-set label on the CUSTOMER. That is the wrong
--   axis: the environment of a request is a property of the APP VARIANT it
--   came from (local build / Vercel preview / production), not of the
--   customer. A real customer can hit a preview deployment; a developer can
--   hit production. tenant.category cannot express that.
--
--   Every app already sends the truth on every Bridge request:
--     X-App-Env: production | preview | development
--   The Bridge now reads it, normalises it (production→prod, preview→staging,
--   development→local) and persists it here. The mode filter switches to
--   this column. tenant.category stays as a concept (still used for
--   invoices — billing is per-customer, not per-environment) and is NOT
--   removed.
--
-- Enum reuse:
--   app_env's value domain (prod/staging/local) is identical to the
--   tenant_category enum from migration 006, and the mode-filter query code
--   already casts the mode param `::tenant_category`. Reusing the type keeps
--   the comparison clean and avoids a second, drift-prone parallel enum for
--   the very same three-value environment classification.
--
-- Existing rows: app_env stays NULL. NO backfill — an old row's environment
-- is genuinely unknown, and guessing it would re-introduce exactly the
-- false-bucketing this migration removes. A mode filter therefore excludes
-- pre-migration rows (honest un-attributed, not silently mis-filed).
--
-- Forward-only / idempotent: ADD COLUMN IF NOT EXISTS + CREATE INDEX
-- IF NOT EXISTS are no-ops once applied. No BEGIN/COMMIT — the migration
-- runner wraps each file in a single transaction.

ALTER TABLE activities
    ADD COLUMN IF NOT EXISTS app_env tenant_category;

CREATE INDEX IF NOT EXISTS idx_activities_app_env ON activities(app_env);
