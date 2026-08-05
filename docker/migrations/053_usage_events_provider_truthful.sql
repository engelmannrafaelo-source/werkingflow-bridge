-- 053 — usage_events.provider sagt die Wahrheit (Rafael, 2026-08-05)
--
-- usage_events.provider beantwortet eine Compliance-Frage, bevor es eine
-- Abrechnungsfrage ist: WER hat die Daten dieses Calls physisch bekommen?
-- Die Spalte ist der Beleg hinter der Kundenzusage "keine Uebermittlung an
-- Anthropic USA" (Datenschutz-Fassung 3.1, Kainer-AVV).
--
-- Bis hierher trug sie DEFAULT 'anthropic'. Jeder Schreiber, der die Frage
-- nicht beantwortet hat, bekam "anthropic" geschenkt — auch fuer Calls, die
-- unsere Infrastruktur nie verlassen haben, und fuer Calls an voellig andere
-- Firmen. Gemessen vor dem Fix:
--   dev : 27.292 von 69.321 Zeilen (39%) — docling/html-renderer/privacy-service
--   prod:    957 von  5.209 Zeilen (18%) — dieselben drei Service-Modelle
-- Dazu im Code (nicht in diesen Zahlen enthalten, weil STT bisher ungenutzt):
-- Transkription an OpenAI Whisper und Calls ueber Gemini-CLI /
-- OpenAI-compatible wurden ebenfalls als 'anthropic' verbucht.
--
-- Der Schaden geht in BEIDE Richtungen: die Spalte erfindet
-- Anthropic-Uebermittlungen, die nie stattfanden (und laesst die US-Exposition
-- groesser aussehen als sie ist), und sie versteckt gleichzeitig echte
-- Dritt-Verarbeiter hinter dem Anthropic-Label. Fuer ein Auditdokument ist
-- beides untragbar.
--
-- Diese Migration macht drei Dinge:
--   1. DEFAULT weg — die DB erfindet keinen Provider mehr. Zusammen mit dem
--      jetzt pflichtigen provider-Argument in persist_ai_call_activity kann
--      keine Zeile mehr ohne bewusste Entscheidung entstehen.
--   2. Backfill der BEWEISBAR falschen Zeilen. Nur die drei Service-Modelle,
--      die per Definition lokal laufen (docling = PDF->Markdown,
--      html-renderer = PDF/Screenshot, privacy-service = Anonymisierung) und
--      per Definition 0,00 EUR kosten. Kein Abrechnungs-Effekt, keine
--      Reconciliation-Verschiebung — nur ein Label, das aufhoert zu luegen.
--      Fehlerzeilen werden NICHT rueckwirkend angefasst: bei denen ist pro
--      Zeile nicht mehr beweisbar, ob geroutet wurde. Lieber unscharf als
--      falsch-praezise.
--   3. CHECK-Constraint auf das Vokabular aus src/activity/providers.py.
--      Ein unbekannter Wert scheitert am Insert statt still im Ledger zu
--      landen. dev und prod enthalten vor der Migration ausschliesslich
--      anthropic/bedrock/research-cloud, der Constraint validiert also sauber.
--
-- Vorwaerts-only und idempotent (mehrfach ausfuehrbar).

ALTER TABLE usage_events ALTER COLUMN provider DROP DEFAULT;

-- Nur Zeilen anfassen, die noch die Unwahrheit behaupten -> Re-Run ist No-op.
UPDATE usage_events
   SET provider = 'local'
 WHERE provider = 'anthropic'
   AND model IN ('docling', 'html-renderer', 'privacy-service');

ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_provider_vocabulary;
ALTER TABLE usage_events ADD CONSTRAINT usage_events_provider_vocabulary CHECK (
    provider IN (
        -- extern: Daten haben unsere Infrastruktur verlassen
        'anthropic', 'bedrock', 'research-cloud',
        'openai', 'aws-sagemaker', 'openai-compatible', 'gemini',
        -- nicht extern
        'local',      -- auf eigener Infrastruktur gerechnet, kein Provider-Call
        'unrouted',   -- vor der Backend-Wahl abgelehnt (Gate/Budget), nichts gesendet
        'unknown'     -- Sentinel des Writers: sichtbar falsch statt plausibel falsch
    )
);

COMMENT ON COLUMN usage_events.provider IS
    'Wer hat die Daten physisch bekommen. Vokabular + Begruendung: '
    'src/activity/providers.py. Fuer Datenschutz-Auswertungen NICHT auf '
    'provider=''anthropic'' allein filtern und NICHT ungefiltert ueber alle '
    'Zeilen: EXTERNAL_PROVIDERS ist die Menge, die Kundendaten erhalten hat.';
