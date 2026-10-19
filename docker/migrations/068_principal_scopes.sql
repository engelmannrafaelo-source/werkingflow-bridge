-- App grants never confer administration. Existing wildcard/multi-app identities
-- need deliberate reprovisioning; no migration automatically promotes an admin.
ALTER TABLE service_principals ADD COLUMN IF NOT EXISTS scopes TEXT[] NOT NULL DEFAULT '{}';
UPDATE service_principals SET scopes = ARRAY['app:' || allowed_apps[1]]
WHERE cardinality(allowed_apps) = 1 AND allowed_apps[1] <> '*'
  AND scopes = '{}';
-- Admin bootstrap is an explicit offline provisioning operation, not an HTTP
-- escape hatch. Only that separate principal may have {bridge:admin}, apps {}.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'service_principals_separate_grants') THEN
        ALTER TABLE service_principals ADD CONSTRAINT service_principals_separate_grants CHECK (
            scopes = '{}' OR
            (scopes = ARRAY['bridge:admin'] AND cardinality(allowed_apps) = 0) OR
            (cardinality(allowed_apps) = 1 AND allowed_apps[1] <> '*'
             AND scopes = ARRAY['app:' || allowed_apps[1]])
        );
    END IF;
END $$;
