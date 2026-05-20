-- Phase 9 — app_env: the environment a request actually came from.
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
-- Dedicated enum (NOT a reuse of tenant_category):
--   app_env's value domain (prod/staging/local) happens to share the same
--   three labels as the tenant_category enum from migration 006, but the
--   two are independent axes: app_env is "which environment did this
--   request come from" (per-row, derived from X-App-Env); tenant_category
--   is "what kind of customer is this" (per-tenant, hand-set, drives
--   billing). Coupling them to a single enum type would mean a future
--   change to one axis' value domain silently reshapes the other, and lets
--   accidental cross-axis comparisons type-check as valid. A dedicated
--   `app_env` enum keeps the two axes decoupled and self-documenting (the
--   column reads `app_env app_env`, mirroring the existing `app_id app_id`).
--
-- Tables: app_env is added to BOTH activities (ai-call rows) and feedback
--   (user-submitted feedback rows). Each captures X-App-Env at write time
--   via the same helper (normalize_app_env). The "mode" filter on both
--   reads this column instead of joining tenant.category.
--
-- Existing rows: app_env stays NULL. NO backfill — an old row's environment
-- is genuinely unknown, and guessing it would re-introduce exactly the
-- false-bucketing this migration removes. A mode filter therefore excludes
-- pre-migration rows (honest un-attributed, not silently mis-filed).
--
-- Forward-only / idempotent: the enum is created via a duplicate_object-
-- swallowing DO block (CREATE TYPE has no IF NOT EXISTS); ADD COLUMN and
-- CREATE INDEX use IF NOT EXISTS and are no-ops once applied. No
-- BEGIN/COMMIT — the migration runner wraps each file in a single
-- transaction.

DO $$ BEGIN
    CREATE TYPE app_env AS ENUM ('prod', 'staging', 'local');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE activities
    ADD COLUMN IF NOT EXISTS app_env app_env;

CREATE INDEX IF NOT EXISTS idx_activities_app_env ON activities(app_env);

ALTER TABLE feedback
    ADD COLUMN IF NOT EXISTS app_env app_env;

CREATE INDEX IF NOT EXISTS idx_feedback_app_env ON feedback(app_env);
