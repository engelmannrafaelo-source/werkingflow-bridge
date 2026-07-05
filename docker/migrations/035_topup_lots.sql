-- 035_topup_lots.sql
-- TopUp guthaben als datierte Lots (Budget-Modell final, 2026-07-05).
--
-- Bisher lag das app-übergreifende TopUp-Guthaben als EIN Skalar in
-- user_topup_balances.balance_eur — ohne Kaufdatum, ohne Verfall. Das
-- widerspricht dem finalen Budget-Modell (packages/usage-billing-admin/docs/
-- BUDGET-MODELL.md, Regel 6): TopUp ist das fungible Geld, wird als datierte
-- Lots geführt, verfällt 12 Monate nach Kauf und wird FIFO (ältester Kauf
-- zuerst) abgebucht. Das Ablaufdatum ist Bilanz-Verbindlichkeit + Preis-Drift-
-- Bremse und muss dem Kunden sichtbar sein ("X € · gültig bis …").
--
-- Diese Tabelle gibt jedem TopUp-Kauf sein eigenes datiertes Lot:
--   amount_eur   = VERBLEIBENDER Betrag (FIFO-reduziert bei Abbuchung)
--   purchased_at = Kaufzeitpunkt (Quelle: credit_purchases.paid_at)
--   expires_at   = purchased_at + 12 Monate
--
-- Die vollständige Kaufhistorie bleibt in credit_purchases (Audit); ein Lot
-- verweist optional per credit_purchase_id darauf.
--
-- MIGRATION DES BESTANDS (bewusst NICHT hier): user_topup_balances.balance_eur
-- ist ein bereits abgebuchter Skalar — die FIFO-Historie lässt sich daraus
-- nicht verlustfrei rekonstruieren (Kaufdaten liegen in credit_purchases, aber
-- welche Lots historisch schon verbraucht wurden, ist nicht abgeleitet). Das
-- Backfill erfordert eine bewusste Entscheidung (siehe Report an Rafael) und
-- wird NICHT still hier ausgeführt. Diese Migration legt nur die Struktur an;
-- das Laufzeit-Loading fail-loud't auf einen nicht-migrierten Skalar-Restwert,
-- damit unsichtbar gemachtes Kundengeld sofort auffällt statt still zu
-- verschwinden.
--
-- Forward-only, idempotent.

CREATE TABLE IF NOT EXISTS user_topup_lots (
    id                 UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Verbleibender Betrag dieses Lots (FIFO-reduziert). 0 = leergebucht.
    amount_eur         NUMERIC(12, 4) NOT NULL CHECK (amount_eur >= 0),
    -- Kaufzeitpunkt (== credit_purchases.paid_at). Kein DEFAULT: ein Lot ohne
    -- realen Kaufzeitpunkt ist ein Fehler, kein zu-ratender Sonderfall.
    purchased_at       TIMESTAMPTZ   NOT NULL,
    -- Verfall: purchased_at + 12 Monate. Vom Ersteller berechnet, nicht geraten.
    expires_at         TIMESTAMPTZ   NOT NULL,
    -- Audit-Verweis auf den zugrunde liegenden Kauf (NULL bei manuellem Credit).
    credit_purchase_id UUID,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    -- Verfall muss nach dem Kauf liegen (12-Monate-Regel, nie invertiert).
    CONSTRAINT user_topup_lots_expiry_after_purchase CHECK (expires_at > purchased_at)
);

-- Hot path: FIFO-Reihenfolge je User (ältester Kauf zuerst, dann Verfall).
CREATE INDEX IF NOT EXISTS idx_user_topup_lots_fifo
    ON user_topup_lots (user_id, purchased_at, expires_at);
