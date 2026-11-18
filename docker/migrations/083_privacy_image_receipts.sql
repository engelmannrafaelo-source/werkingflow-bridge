-- 083 — Bild-Receipts der Bild-PII-Maske (Bildvertrag v3 §2.1, Schnittstelle Bild v3; c9a50d3b, 18.11.2026).
-- Nur Metadaten, NIE Bildbytes. Ein final_token ergibt genau einen Receipt (token_hash UNIQUE, A-SS-3).
-- Verbrauch genau einmal und nur zusammen mit der Job-Anlage (consumed_job_id), siehe src/jobs/store.py.
CREATE TABLE IF NOT EXISTS privacy_image_receipts (
    receipt_id        UUID PRIMARY KEY,
    token_hash        CHAR(64) NOT NULL UNIQUE,
    sha256            CHAR(64) NOT NULL,
    mime              TEXT NOT NULL CHECK (mime IN ('image/png', 'image/jpeg')),
    principal         TEXT NOT NULL,
    tenant            TEXT NOT NULL,
    app_id            TEXT NOT NULL,
    breite            INTEGER NOT NULL CHECK (breite > 0),
    hoehe             INTEGER NOT NULL CHECK (hoehe > 0),
    leer              BOOLEAN NOT NULL,
    automasken_anzahl INTEGER NOT NULL CHECK (automasken_anzahl >= 0),
    handmasken_anzahl INTEGER NOT NULL CHECK (handmasken_anzahl >= 0),
    bestaetigung_version TEXT NOT NULL,
    bestaetigt_um     TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    consumed_job_id   TEXT NULL,
    consumed_at       TIMESTAMPTZ NULL,
    CHECK ((consumed_job_id IS NULL) = (consumed_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS privacy_image_receipts_consumed_job_uq
    ON privacy_image_receipts (consumed_job_id) WHERE consumed_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS privacy_image_receipts_expires_idx ON privacy_image_receipts (expires_at);
