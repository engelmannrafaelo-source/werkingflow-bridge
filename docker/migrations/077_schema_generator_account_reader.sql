-- Add only the identity-existence read to the already enrolled Dev generator.
-- No token, app, budget, activation or customer-entitlement changes.
DO $$
DECLARE p service_principals%ROWTYPE;
BEGIN
  SELECT * INTO p FROM service_principals WHERE name = 'schema-generator-dev' FOR UPDATE;
  IF NOT FOUND THEN RETURN; END IF; -- no Dev-only principal on Prod
  IF NOT p.active OR p.allowed_apps IS DISTINCT FROM ARRAY['werking-schema-generator']::text[] THEN
    RAISE EXCEPTION 'schema-generator-dev identity differs from approved enrollment';
  END IF;
  IF cardinality(p.allowed_paths) = 2 AND p.allowed_paths @> ARRAY['/v1/chat/completions', '/v1/auth/account-exists']::text[] THEN RETURN; END IF;
  IF p.allowed_paths IS DISTINCT FROM ARRAY['/v1/chat/completions']::text[] THEN
    RAISE EXCEPTION 'schema-generator-dev paths differ from approved baseline';
  END IF;
  UPDATE service_principals
     SET allowed_paths = ARRAY['/v1/chat/completions', '/v1/auth/account-exists']::text[]
   WHERE id = p.id;
END $$;
