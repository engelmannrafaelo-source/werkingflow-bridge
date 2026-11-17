-- 080: Atomare Reservierung fuer strict_monthly (oeffentlicher Schema-Generator), Root-Klarstellung 16.11.2026.
-- Siehe src/budget/strict_reservation.py. Rein additiv: neue Tabelle, keine Aenderung bestehender Daten.
-- Gelesen/geschrieben nur unter der user_budgets-Zeilensperre (FOR UPDATE) des jeweiligen Users.
-- Umbau nach Pruefung R3: Reservierung = Verbrauch bis zur Abrechnung; keine Freigabe, kein Ablauf.
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id uuid PRIMARY KEY,
    user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id        text NOT NULL,
    amount_eur     numeric(12,6) NOT NULL CHECK (amount_eur > 0),
    status         text NOT NULL DEFAULT 'offen' CHECK (status IN ('offen', 'abgerechnet')),
    settled_eur    numeric(12,6),
    created_at     timestamptz NOT NULL DEFAULT now(),
    closed_at      timestamptz
);
CREATE INDEX IF NOT EXISTS budget_reservations_offen_idx
    ON budget_reservations (user_id, plan_id, created_at) WHERE status = 'offen';
