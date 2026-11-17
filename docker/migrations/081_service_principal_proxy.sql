-- 081 — Proxy-Principals (ProxyV2, Bridge-Owner 51c8185c, 17.11.2026; SEC-GO cbb399b73929)
--
-- Ein Proxy-Principal handelt ueber X-Bridge-Proxy-Token fuer GENAU EINEN Nutzer
-- (proxy_user_id) in GENAU EINEM Mandanten (proxy_tenant_id = serverseitige
-- tenants.id) bis proxy_expires_at, nur auf den Routen in proxy_routes
-- ("METHODE /vorlage"; der Code deckelt zusaetzlich auf eine feste Obergrenze).
-- Er ist nie Operator und nie KI-Principal (die KI-Aufloesung filtert
-- cardinality(proxy_routes) = 0). Bestandszeilen: proxy_routes = '{}' => unveraendert.
ALTER TABLE service_principals
    ADD COLUMN IF NOT EXISTS proxy_routes     TEXT[]       NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS proxy_user_id    UUID         NULL REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS proxy_tenant_id  VARCHAR(128) NULL REFERENCES tenants(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS proxy_expires_at TIMESTAMPTZ  NULL;

-- Fail-closed auf Schemaebene: eine Proxy-Zeile hat Nutzer, Mandant und Ablauf
-- bei der Anlage, genau eine App ohne '*', keinen '*'-Pfad und keine '*'-Route.
ALTER TABLE service_principals DROP CONSTRAINT IF EXISTS service_principals_proxy_form;
ALTER TABLE service_principals ADD CONSTRAINT service_principals_proxy_form CHECK (
    cardinality(proxy_routes) = 0
    OR (
        proxy_expires_at IS NOT NULL
        AND cardinality(allowed_apps) = 1
        AND NOT ('*' = ANY (allowed_apps))
        AND NOT ('*' = ANY (allowed_paths))
        AND cardinality(allowed_paths) > 0
        AND NOT ('*' = ANY (proxy_routes))
    )
) NOT VALID;
ALTER TABLE service_principals VALIDATE CONSTRAINT service_principals_proxy_form;
