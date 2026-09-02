-- 059 — usage_events bekommt eine dedizierte Fehlerspalte (Rafael/DevOps, 2026-09-02)
--
-- Beleg (DevOps, 2026-09-01): eine 402- (Guthaben leer) oder 429-Antwort ist im
-- Ledger nicht als eigene, indexierbare Groesse abfragbar. status/error_code
-- werden pro Call schon seit Migration 016/`ai_call_writer.py` geschrieben —
-- aber nur in provider_metadata (JSONB), nie in eine eigene Spalte. Jede
-- Monitoring-/Kundenaktivitaets-Abfrage, die nach Fehlern filtern oder ueber
-- viele Zeilen aggregieren will, muss dafuer `provider_metadata->>'status'`
-- extrahieren statt einen Spaltenindex zu nutzen — funktional korrekt, aber
-- weder indexierbar noch ein stabiler Vertrag (Tippfehler im JSON-Key faellt
-- nie auf).
--
-- Diese Migration promoted status + error_code zu echten Spalten:
--   1. status TEXT NOT NULL DEFAULT 'success', Vokabular ('success'|'error')
--      per CHECK. Backfill aus provider_metadata->>'status' fuer bestehende
--      Zeilen; fehlt der Key (Zeilen von vor der status-Verfolgung), gilt
--      'success' als korrekte Annahme — das Ledger hat schon immer nur
--      abgeschlossene Calls verbucht, Fehler-Calls tragen status seit ihrer
--      Einfuehrung immer mit.
--   2. error_code TEXT NULL, Backfill aus provider_metadata->>'error_code'.
--   3. Zwei Indizes fuer die neuen Monitoring-Abfragen: ein Teilindex auf
--      Nicht-Erfolgs-Zeilen (klein, da status='success' die grosse Mehrheit
--      ist) und ein Teilindex auf error_code fuer Filterung nach Fehlerart.
--
-- Rueckwaertskompatibel: additive Spalten, kein Rename/Drop. provider_metadata
-- behaelt status/error_code weiterhin (kein zweiter Schreibpfad noetig) — die
-- neuen Spalten sind eine parallele, indexierte Sicht auf dieselbe Tatsache.
-- 'rejected' (fuer Pre-Call-Ablehnungen wie der 402-Budget-Gate, die heute GAR
-- KEINE Zeile schreiben) ist bewusst NICHT im Vokabular: das Schreiben einer
-- Zeile fuer abgelehnte Calls ist ein separater, noch nicht entschiedener
-- Schreibpfad-Change (siehe Nachtbericht) — die CHECK-Constraint hier deckt
-- nur, was der Writer heute tatsaechlich erzeugt.
--
-- Vorwaerts-only und idempotent (mehrfach ausfuehrbar).

ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS error_code TEXT;

UPDATE usage_events
   SET status = COALESCE(provider_metadata->>'status', 'success')
 WHERE status IS NULL;

UPDATE usage_events
   SET error_code = provider_metadata->>'error_code'
 WHERE error_code IS NULL AND provider_metadata ? 'error_code';

ALTER TABLE usage_events ALTER COLUMN status SET DEFAULT 'success';
ALTER TABLE usage_events ALTER COLUMN status SET NOT NULL;

ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_status_vocabulary;
ALTER TABLE usage_events ADD CONSTRAINT usage_events_status_vocabulary CHECK (
    status IN ('success', 'error')
);

CREATE INDEX IF NOT EXISTS idx_usage_events_status_recorded
    ON usage_events(status, recorded_at DESC)
    WHERE status <> 'success';

CREATE INDEX IF NOT EXISTS idx_usage_events_error_code
    ON usage_events(error_code)
    WHERE error_code IS NOT NULL;

COMMENT ON COLUMN usage_events.status IS
    'success|error — Ergebnis DES CALLS, der eine Zeile geschrieben hat. '
    'Deckt NICHT Pre-Call-Ablehnungen (z.B. 402 Budget-Gate) ab, die vor '
    'persist_ai_call_activity() abbrechen und heute keine Zeile erzeugen — '
    'siehe src/budget/gate.py.';
COMMENT ON COLUMN usage_events.error_code IS
    'HTTP-Status oder Fehlerklasse des Calls (z.B. "429", "stream_aborted"), '
    'NULL bei status=''success''. Quelle: src/activity/ai_call_writer.py.';
