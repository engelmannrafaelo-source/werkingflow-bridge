-- 068 — Betragsbasis je Mollie-Abschluss: Abo und Top-Up ziehen brutto ein,
-- aber nur bei NEUEN Abschluessen.
--
-- Rafael-Entscheid 18.10.2026 (e-bridge-abos-brutto = a): "ja, aber erst fuer
-- neue Abschluesse; bestehende Abos bleiben unveraendert". Die Markierung sitzt
-- am Abschluss, nicht am Deploy-Zeitpunkt: eine vor dem Umstieg gestartete,
-- danach bezahlte Zahlung bleibt so, wie sie dem Kunden angezeigt wurde.
--
--   pending_payments.amount_basis  'net' | 'gross' — wie amount_eur (der bei
--       Mollie angelegte Betrag) zu lesen ist. Default 'net' beschreibt jeden
--       Bestandsdatensatz von Abo und Top-Up genau. Self-Service-Pakete
--       (project_pack) zogen schon seit 17.10. brutto ein; ihr Betrag kommt aus
--       der Bestell-Rechnung, der neue Code schreibt fuer sie 'gross'.
--   pending_payments.net_eur       Nettobetrag des Abschlusses (Abo: Planpreis x
--       Sitze, Top-Up: Guthaben). Pflicht, sobald ein Abo/Top-Up brutto
--       angelegt ist — sonst waere die Gutschrift aus dem Bruttobetrag zu raten.
--   subscriptions.amount_basis     Basis, mit der das Mollie-Abo dieses Abos
--       angelegt wurde (Mollie bucht diesen Betrag monatlich weiter ab).
--       Default 'net' = jedes Bestands-Abo.
--   subscriptions.net_eur          Monatsnetto (Rechnung jeder Folgezahlung).
--
-- Alter Code liest keine der Spalten und schreibt ueber die Defaults den
-- Bestand; Migration vor dem Code ist deshalb gefahrlos (Deploy-Gate 3.7).

ALTER TABLE pending_payments
    ADD COLUMN IF NOT EXISTS amount_basis TEXT NOT NULL DEFAULT 'net',
    ADD COLUMN IF NOT EXISTS net_eur NUMERIC(8,2);

ALTER TABLE pending_payments
    DROP CONSTRAINT IF EXISTS pending_payments_amount_basis_check;
ALTER TABLE pending_payments
    ADD CONSTRAINT pending_payments_amount_basis_check
    CHECK (amount_basis IN ('net', 'gross'));

ALTER TABLE pending_payments
    DROP CONSTRAINT IF EXISTS pending_payments_brutto_braucht_netto;
ALTER TABLE pending_payments
    ADD CONSTRAINT pending_payments_brutto_braucht_netto
    CHECK (
        type NOT IN ('subscription_first', 'topup')
        OR amount_basis = 'net'
        OR (net_eur IS NOT NULL AND net_eur > 0)
    );

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS amount_basis TEXT NOT NULL DEFAULT 'net',
    ADD COLUMN IF NOT EXISTS net_eur NUMERIC(8,2);

ALTER TABLE subscriptions
    DROP CONSTRAINT IF EXISTS subscriptions_amount_basis_check;
ALTER TABLE subscriptions
    ADD CONSTRAINT subscriptions_amount_basis_check
    CHECK (amount_basis IN ('net', 'gross'));

-- subscriptions.net_eur: Nettobetrag je Monat (Rechnung jeder Folgezahlung).
-- Pflicht bei gross — sonst waere das Netto aus dem Bruttobetrag zu raten.
-- NULL bei Bestands-Abos (net): deren Folgerechnung geht vom bezahlten Betrag aus.
ALTER TABLE subscriptions
    DROP CONSTRAINT IF EXISTS subscriptions_brutto_braucht_netto;
ALTER TABLE subscriptions
    ADD CONSTRAINT subscriptions_brutto_braucht_netto
    CHECK (amount_basis = 'net' OR (net_eur IS NOT NULL AND net_eur > 0));
