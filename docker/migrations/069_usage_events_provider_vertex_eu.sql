-- 069: usage_events.provider um 'vertex-eu' erweitern.
--
-- Claude ueber Google Vertex AI, EU-Multiregion (backend=vertex_eu,
-- src/vertex_eu_service.py, vorgebaut 21.10.2026). Daten verlassen unsere
-- Infrastruktur Richtung Google (locations/eu); 'gemini' waere falsch
-- (anderes Modell, andere Abrechnung, andere Residenz-Zusage). Rein additiv.
-- MUSS eingespielt sein, BEVOR VERTEX_EU_ENABLED irgendwo eingeschaltet wird,
-- sonst scheitern die Ledger-Inserts am CHECK und bleiben im Spool offen.

ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_provider_vocabulary;
ALTER TABLE usage_events ADD CONSTRAINT usage_events_provider_vocabulary CHECK (
    provider IN (
        -- extern: Daten haben unsere Infrastruktur verlassen
        'anthropic', 'bedrock', 'research-cloud',
        'openai', 'aws-sagemaker', 'openai-compatible', 'gemini',
        'convertapi',
        'vertex-eu',
        -- nicht extern
        'local',
        'unrouted',
        'unknown'
    )
);
