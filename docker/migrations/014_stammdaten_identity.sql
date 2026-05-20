-- Stammdaten für Tenant (Firmen-Identität) + User (Gutachter-Identität).
--
-- Entscheidung: Tenant-Stammdaten = Bridge validiert das Firma-Schema
-- (app-übergreifend universell). User-Stammdaten = opaker JSONB-Blob
-- (werking-report-Domäne; Bridge speichert, werking-report validiert).
--
-- Konsequenz: kein werking-report-Domänenvokabular im zentralen Bridge-Schema
-- und kein TS→Python-Schema-Spiegel, der driften kann.

CREATE TABLE IF NOT EXISTS tenant_stammdaten (
    tenant_id      VARCHAR(128) PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    firma          JSONB        NOT NULL DEFAULT '{}',
    logo           TEXT,                                   -- opaker String (data-URI oder URL)
    style_settings JSONB        NOT NULL DEFAULT '{}',
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_by     UUID         REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS user_stammdaten (
    user_id    UUID         PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data       JSONB        NOT NULL DEFAULT '{}',         -- opaker Gutachter-Blob (werking-report-Domäne)
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
