-- Phase 10 — tenant.category → tenant.account_type rename.
--
-- Why this rename:
--   Migration 006 introduced `tenants.category` (enum tenant_category, values
--   prod/staging/local) to classify CUSTOMERS — a hand-set label on who the
--   tenant is. Migration 009 added a separate axis, `app_env`, that captures
--   the ENVIRONMENT a request came from (per-call, derived from X-App-Env).
--
--   The two axes are independent — "what kind of customer" vs "where did
--   this request originate" — but they happen to share the same three
--   labels (prod/staging/local). That collision invites confusion every
--   time a filter, comment, or admin label says "mode: prod" (which one?).
--
--   This migration renames the customer-axis column + enum to read for
--   what it actually is:
--     type tenant_category      → account_type
--     values prod/staging/local → customer/test/internal
--     column tenants.category   → tenants.account_type
--   Value mapping is structural: prod→customer, staging→test, local→internal.
--   The app_env enum is untouched — its three labels stay prod/staging/local
--   because they describe environments.
--
-- External consumer warning:
--   This rename touches the wire. Endpoints that returned `category` /
--   `tenant_category` and accepted `mode=prod|staging|local` now use
--   `account_type` / `customer|test|internal`. Coordinate consumers
--   (`usage-billing-admin` package + `apps/partner-platform` Tenants tab)
--   BEFORE applying this migration in any environment that those consumers
--   read from.
--
-- Idempotency:
--   The migration runner (bin/bridge-migrate.sh) wraps each file in a single
--   transaction and tracks applied filenames by sha256 — so a re-run is a
--   no-op at the runner level. The per-step guards below additionally make
--   this file safe to re-run by hand against a half-migrated DB (e.g. after
--   a partial restore): each step checks the actual catalog state and skips
--   if already done.
--
-- Forward-only: no BEGIN/COMMIT here — the runner provides the transaction.
--   `ALTER TYPE … RENAME VALUE` is transactional in Postgres 12+, so this
--   whole file commits or rolls back atomically together with the
--   schema_migrations bookkeeping insert.

-- Step 1 — rename enum values. Each guarded by "old label still present".
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname IN ('tenant_category', 'account_type')
          AND e.enumlabel = 'prod'
    ) THEN
        EXECUTE format(
            'ALTER TYPE %I RENAME VALUE ''prod'' TO ''customer''',
            (SELECT typname FROM pg_type WHERE typname IN ('tenant_category', 'account_type') LIMIT 1)
        );
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname IN ('tenant_category', 'account_type')
          AND e.enumlabel = 'staging'
    ) THEN
        EXECUTE format(
            'ALTER TYPE %I RENAME VALUE ''staging'' TO ''test''',
            (SELECT typname FROM pg_type WHERE typname IN ('tenant_category', 'account_type') LIMIT 1)
        );
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname IN ('tenant_category', 'account_type')
          AND e.enumlabel = 'local'
    ) THEN
        EXECUTE format(
            'ALTER TYPE %I RENAME VALUE ''local'' TO ''internal''',
            (SELECT typname FROM pg_type WHERE typname IN ('tenant_category', 'account_type') LIMIT 1)
        );
    END IF;
END $$;

-- Step 2 — rename the type itself. Skip if either nothing to do or already done.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tenant_category')
       AND NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'account_type') THEN
        ALTER TYPE tenant_category RENAME TO account_type;
    END IF;
END $$;

-- Step 3 — rename the column on tenants.
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'tenants' AND column_name = 'category'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'tenants' AND column_name = 'account_type'
    ) THEN
        ALTER TABLE tenants RENAME COLUMN category TO account_type;
    END IF;
END $$;

-- Step 4 — rename the index (006 created `idx_tenants_category`).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_tenants_category')
       AND NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_tenants_account_type') THEN
        ALTER INDEX idx_tenants_category RENAME TO idx_tenants_account_type;
    END IF;
END $$;

-- Step 5 — re-state the column default with the new label.
-- ALTER TYPE … RENAME VALUE keeps the existing default expression valid
-- (Postgres stores enum defaults by oid), but the cached pg_attrdef text
-- may still print 'prod'::tenant_category. Re-setting makes the dumped
-- DDL match the new vocabulary (and is a no-op on re-run).
ALTER TABLE tenants ALTER COLUMN account_type SET DEFAULT 'customer';
