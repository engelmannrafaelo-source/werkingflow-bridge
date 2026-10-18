-- 071 — Einmaligkeit eines Zahlungsereignisses als Datenbank-Invariante.
--
-- Codex R3 18.10.2026: die Ereignisse payment.amount_mismatch,
-- invoice.net_basis_vat_from_payment und payment.refund_unbooked wurden per
-- SELECT-vor-INSERT dedupliziert — seriell einmalig, bei parallelen Webhooks
-- zur selben Zahlung nicht. Jetzt traegt jedes solche Ereignis einen
-- fachlichen Schluessel, die Datenbank haelt ihn eindeutig, und der Code
-- schreibt mit INSERT ... ON CONFLICT DO NOTHING VOR jeder weiteren Wirkung.
--
--   billing_events.ereignis_schluessel  NULL fuer alle uebrigen Ereignisse
--       (Append-only-Protokoll wie bisher). Aufbau, siehe
--       billing_service.ereignis_schluessel():
--         payment.amount_mismatch:<payment_id>
--         invoice.net_basis_vat_from_payment:<payment_id>
--         payment.refund_unbooked:<payment_id>:<erstattet>:<rueckgebucht>
--
-- Nummer 071: 069/070 sind auf Zweig fix/bridge-planwechsel-consent-20261018
-- vergeben. Alter Code liest und schreibt die Spalte nicht; Migration vor dem
-- Code ist gefahrlos. Der Nachtrag unten vergibt bestehenden Zeilen dieser
-- drei Arten den Schluessel (je Schluessel die aelteste Zeile), damit eine
-- Wiederholung nach dem Deploy kein zweites Ereignis schreibt. Die Datei ist
-- wiederholbar (IF NOT EXISTS, Nachtrag nur fuer noch nicht vergebene Schluessel).

ALTER TABLE billing_events
    ADD COLUMN IF NOT EXISTS ereignis_schluessel TEXT;

UPDATE billing_events b
   SET ereignis_schluessel = k.schluessel
  FROM (
        SELECT id, schluessel,
               ROW_NUMBER() OVER (PARTITION BY schluessel ORDER BY timestamp, id) AS nr
          FROM (
                SELECT id, timestamp,
                       CASE event_type
                         WHEN 'payment.refund_unbooked' THEN
                           event_type || ':' || mollie_payment_id || ':'
                           || COALESCE(payload->>'refundedEur', '') || ':'
                           || COALESCE(payload->>'chargedBackEur', '')
                         ELSE event_type || ':' || mollie_payment_id
                       END AS schluessel
                  FROM billing_events
                 WHERE event_type IN ('payment.amount_mismatch',
                                      'invoice.net_basis_vat_from_payment',
                                      'payment.refund_unbooked')
                   AND mollie_payment_id IS NOT NULL
                   AND ereignis_schluessel IS NULL
               ) roh
       ) k
 WHERE b.id = k.id AND k.nr = 1
   -- erneuter Lauf: einen schon vergebenen Schluessel nie ein zweites Mal setzen
   AND NOT EXISTS (SELECT 1 FROM billing_events x WHERE x.ereignis_schluessel = k.schluessel);

CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_events_ereignis_schluessel
    ON billing_events (ereignis_schluessel)
    WHERE ereignis_schluessel IS NOT NULL;
