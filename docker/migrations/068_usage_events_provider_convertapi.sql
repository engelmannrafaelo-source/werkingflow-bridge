-- 068: usage_events.provider um 'convertapi' erweitern.
--
-- Der Word-Export (/v1/convert-html-to-docx) wird seit 20.10.2026 verbucht wie
-- der PDF-Export. Anders als das PDF (eigenes Chromium, provider='local')
-- verlaesst das Dokument dabei unsere Infrastruktur: ConvertAPI-SaaS
-- (Litauen). 'local' waere im Compliance-Ledger falsch (siehe 053 und
-- src/activity/providers.py). Rein additiv: bestehende Zeilen bleiben gueltig.
-- MUSS vor dem Bridge-Code mit PROVIDER_CONVERTAPI eingespielt sein, sonst
-- scheitern die Inserts am CHECK und bleiben im Spool offen.

ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_provider_vocabulary;
ALTER TABLE usage_events ADD CONSTRAINT usage_events_provider_vocabulary CHECK (
    provider IN (
        -- extern: Daten haben unsere Infrastruktur verlassen
        'anthropic', 'bedrock', 'research-cloud',
        'openai', 'aws-sagemaker', 'openai-compatible', 'gemini',
        'convertapi',
        -- nicht extern
        'local',
        'unrouted',
        'unknown'
    )
);
