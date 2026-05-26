-- Migration 017 — pending_orders: Rechnungs-Lane für manuell freigegebene Bestellungen.
--
-- Workflow (Variante A — manuelle Freigabe durch Operator):
--   1. Admin klickt "auf Rechnung bestellen" im Portal
--   2. Bridge erstellt Invoice (PDF via WeasyPrint, status='issued')
--   3. Admin lädt PDF herunter, Buchhaltung überweist
--   4. Rafael (Operator) sieht offene Bestellungen, prüft Geldeingang
--   5. Rafael klickt "Freigeben" → Bridge aktiviert Subscription/Credits
--
-- Kein Mollie-Webhook-Pfad hier: Zahlung läuft außerhalb Mollie (Überweisung).
-- invoice_id: die bei Bestellung generierte Rechnung (status='issued' → 'paid' bei Release).
-- plan_id: Snapshot aus plans.py (kein FK — Plans sind Code-SSoT, keine DB-Tabelle).

CREATE TABLE IF NOT EXISTS pending_orders (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID            NOT NULL REFERENCES users(id),
    tenant_id       VARCHAR(128)    NOT NULL REFERENCES tenants(id),
    plan_id         VARCHAR         NOT NULL,
    quantity        INTEGER         NOT NULL DEFAULT 1 CHECK (quantity >= 1),
    total_price_eur NUMERIC(10,2)   NOT NULL,
    status          TEXT            NOT NULL
                        CHECK (status IN ('awaiting_payment','released','expired','cancelled')),
    invoice_id      UUID            NOT NULL REFERENCES invoices(id),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    released_at     TIMESTAMPTZ,
    released_by     UUID            REFERENCES users(id),
    release_note    TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_orders_tenant  ON pending_orders(tenant_id);
CREATE INDEX IF NOT EXISTS idx_pending_orders_status  ON pending_orders(status);
CREATE INDEX IF NOT EXISTS idx_pending_orders_user    ON pending_orders(user_id);
