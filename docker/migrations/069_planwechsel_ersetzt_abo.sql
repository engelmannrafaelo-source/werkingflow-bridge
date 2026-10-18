-- 069 — Planwechsel ohne Zwischenzustand ohne Abo.
--
-- Bis 18.10.2026 kuendigte change_subscription das laufende Abo SOFORT und
-- startete danach den Checkout fuer den neuen Plan. Brach der Checkout ab
-- (oder zahlte der Kunde nie), stand der Kunde ohne Abo da; auf 2c98506 brach
-- der Checkout sogar immer ab (Pflichtfeld consent fehlte → TypeError NACH der
-- Kuendigung).
--
-- Jetzt merkt sich die Erstzahlung des neuen Abos, welches Abo sie ersetzt.
-- Gekuendigt wird das alte erst im Webhook, NACHDEM das neue aktiviert ist.
-- NULL = normaler Neuabschluss (jede Bestandszeile). Alter Code liest die
-- Spalte nicht; Migration vor dem Code ist gefahrlos.

ALTER TABLE pending_payments
    ADD COLUMN IF NOT EXISTS ersetzt_abo_id UUID
        REFERENCES subscriptions(id) ON DELETE SET NULL;

ALTER TABLE pending_payments
    DROP CONSTRAINT IF EXISTS pending_payments_ersetzt_abo_nur_abo;
ALTER TABLE pending_payments
    ADD CONSTRAINT pending_payments_ersetzt_abo_nur_abo
    CHECK (ersetzt_abo_id IS NULL OR type = 'subscription_first');
