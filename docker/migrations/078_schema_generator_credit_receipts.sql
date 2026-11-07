-- No credits, subscriptions, budget limits, prices or keys are created here.
CREATE TABLE IF NOT EXISTS schema_generator_credit_receipts (
    user_id uuid NOT NULL REFERENCES users(id),
    project_id text NOT NULL CHECK (project_id ~ '^schema-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    credit_id uuid NOT NULL REFERENCES manual_project_credits(id),
    receipt jsonb NOT NULL CHECK (jsonb_typeof(receipt) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, project_id)
);

DO $$
DECLARE p service_principals%ROWTYPE;
BEGIN
  SELECT * INTO p FROM service_principals WHERE name = 'schema-generator-dev' FOR UPDATE;
  IF NOT FOUND THEN RETURN; END IF;
  IF p.active IS NOT TRUE OR p.allowed_apps IS DISTINCT FROM ARRAY['werking-schema-generator']::text[] THEN
    RAISE EXCEPTION 'schema-generator-dev identity differs from approved enrollment';
  END IF;
  IF cardinality(p.allowed_paths) = 3 AND p.allowed_paths @> ARRAY['/v1/auth/account-exists','/v1/chat/completions','/v1/billing/schema-generator/credit-consumption']::text[] THEN RETURN; END IF;
  IF (cardinality(p.allowed_paths) = 2 AND p.allowed_paths @> ARRAY['/v1/chat/completions','/v1/auth/account-exists']::text[]) IS NOT TRUE THEN
    RAISE EXCEPTION 'schema-generator-dev paths differ from approved baseline';
  END IF;
  UPDATE service_principals
    SET allowed_paths = ARRAY['/v1/auth/account-exists','/v1/chat/completions','/v1/billing/schema-generator/credit-consumption']::text[]
    WHERE id=p.id;
END $$;
