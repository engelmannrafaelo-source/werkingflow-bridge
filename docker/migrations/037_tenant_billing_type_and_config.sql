-- 037 — Tenant billing_type + globaler Self-Checkout-Schalter.
--
-- billing_type trennt "Kunde bucht selbst" von "Betreiber stellt Rechnung
-- manuell aus" (Sondervereinbarungen). NICHT zu verwechseln mit tenants.billing_mode
-- (Kosten-Abrechnungsart subscription|pay_per_token, Sandbox-Ledger) — das bleibt.
--
--   self_service : Tenant bucht selbst via Mollie-Checkout im Portal.
--   managed      : Betreiber provisioniert + stellt Rechnung manuell (B-Serie).
--
-- Default self_service: Selbst-Registrierung ergibt einen buchbaren Tenant.
-- Ob tatsaechlich gebucht werden kann, haengt ZUSAETZLICH am globalen Schalter
-- platform_config.self_checkout_active (im Beta 'false' -> niemand bucht selbst).

DO $$ BEGIN
    CREATE TYPE billing_type AS ENUM ('self_service', 'managed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS billing_type billing_type NOT NULL DEFAULT 'self_service';

-- Globale Plattform-Konfiguration als key/value (JSONB), damit Schalter ohne
-- Redeploy im Platform-Admin umgelegt werden koennen.
CREATE TABLE IF NOT EXISTS platform_config (
    key         TEXT        PRIMARY KEY,
    value       JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by  TEXT
);

-- Beta-Default: Selbstbuchung global AUS. Umlegen = Go-Live der Selbstbuchung.
INSERT INTO platform_config (key, value)
    VALUES ('self_checkout_active', 'false'::jsonb)
    ON CONFLICT (key) DO NOTHING;
