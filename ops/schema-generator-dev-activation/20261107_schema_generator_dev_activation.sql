-- Explicit dev-only activation, NOT part of automatic all-environment migrations.
-- Run with regular bin/bridge-migrate.sh --dir after matching worker rollout.
-- No budgets, users, subscriptions, identities or keys are modified here.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM service_principals
    WHERE name = 'schema-generator-dev' AND active = TRUE
      AND allowed_apps = ARRAY['werking-schema-generator']::text[]
      AND allowed_paths = ARRAY['/v1/chat/completions']::text[]
      AND monthly_cap_eur IS NULL
  ) THEN RAISE EXCEPTION 'Missing narrowly scoped active dev principal'; END IF;
  IF NOT EXISTS (
    SELECT 1 FROM plans WHERE id = 'schema-generator'
      AND app_id::text = 'werking-schema-generator'
      AND budget_plan_id = 'report-standard' AND sellable = FALSE
      AND price_eur = 0 AND api_budget_eur = 0 AND is_trial = FALSE
      AND interval::text = 'month'
  ) THEN RAISE EXCEPTION 'Schema catalog does not match reviewed Check-pot alias'; END IF;
  UPDATE plans SET is_active = TRUE WHERE id = 'schema-generator';
END $$;
