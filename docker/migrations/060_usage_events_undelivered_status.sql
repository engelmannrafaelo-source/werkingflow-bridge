-- 060 — usage_events kennt "abgerechnet, aber nie angekommen" (DevOps, 2026-09-05)
--
-- Befund 03.09.2026: Ein Gateway-Fehler NACH dem Modelllauf erzeugte keine
-- Fehlerzeile. Der Worker rechnete den Call fertig, schrieb status='success'
-- und lieferte die Antwort erst danach aus — in eine Verbindung, die das
-- Gateway laengst abgeraeumt hatte. Der Aufrufer sah einen 504, das Ledger
-- einen erfolgreichen, bezahlten Call; einmal kostete das 2,16 USD fuer eine
-- Antwort, die nie ankam (KOSTEN-VISION-SEPTEMBER-20260904.md, Abschnitt 8g).
--
-- Nachgestellt am 05.09. auf der dev-Bridge: Client bricht nach 1,5s ab, das
-- Modell rechnet weiter, Zeile status='success', 0,034230 EUR
-- (usage_events, user ledger-delivery-repro@example.com).
--
-- Warum ein DRITTER Wert und nicht 'error':
--   'error' bedeutet im Vokabular von Migration 059 "der Call ist gescheitert"
--   — und genau darum berechnet ai_call_writer.py fuer solche Zeilen 0,00 EUR.
--   Ein nicht zugestellter Call ist das Gegenteil: das Modell HAT gerechnet,
--   der Anbieter stellt es uns in Rechnung, nur die Antwort ging auf dem
--   Rueckweg verloren. Auf 'error' gebucht waere das Geld aus jeder
--   Kostenauswertung verschwunden; auf 'success' belassen behauptet es eine
--   Auslieferung, die es nie gab. 'undelivered' ist der einzige Wert, der
--   beide Tatsachen zugleich sagen kann.
--
-- Leser-Wirkung: Die Teilindizes aus 059 (WHERE status <> 'success') decken
-- den neuen Wert automatisch mit ab. /v1/metrics/* liest is_error ab jetzt als
-- status <> 'success' (src/metrics/routes.py) — die Kostensummen dort zaehlen
-- unabhaengig von is_error, der Betrag bleibt also sichtbar.
--
-- Reihenfolge beim Deploy: Diese Migration MUSS vor dem Worker-Rollout laufen
-- (bin/bridge-migrate.sh vor scripts/bridge-deploy.sh, so wie in der
-- Deploy-Prozedur ohnehin vorgesehen). Andernfalls weist die CHECK-Constraint
-- die neuen Zeilen ab; sie gehen nicht verloren (Write-Ahead-Spool), bleiben
-- aber offen, bis die Migration nachgezogen ist.
--
-- Vorwaerts-only und idempotent (mehrfach ausfuehrbar).

ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_status_vocabulary;
ALTER TABLE usage_events ADD CONSTRAINT usage_events_status_vocabulary CHECK (
    status IN ('success', 'error', 'undelivered')
);

COMMENT ON COLUMN usage_events.status IS
    'success|error|undelivered — Ergebnis DES CALLS, der eine Zeile geschrieben '
    'hat. success = Antwort ausgeliefert. error = Call gescheitert, Kosten 0. '
    'undelivered = Modell hat gerechnet und wird berechnet, die Antwort kam '
    'beim Aufrufer aber nie an (Gateway-Timeout/Client-Abbruch; error_code '
    '"caller_gone"). Deckt NICHT Pre-Call-Ablehnungen (z.B. 402 Budget-Gate) '
    'ab, die vor persist_ai_call_activity() abbrechen — siehe src/budget/gate.py.';
