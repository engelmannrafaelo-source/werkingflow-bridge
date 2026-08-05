-- 054 — Kaufzustimmung wird zum Beweisstück (Rafael, 2026-08-05)
--
-- Vor dieser Migration existierte die Zustimmung des Käufers ausschliesslich
-- im Browser. Das Frontend baute ein PurchaseConsent-Objekt (Fassung, beide
-- Erklärungen, Zeitpunkt), prüfte es mit assertPurchaseConsent — und schickte
-- dann nur planId/quantity/successRedirect an die Bridge. Das Objekt wurde
-- verworfen.
--
-- Damit gab es KEINEN Nachweis, dass ein Kunde je zugestimmt hat: kein Feld an
-- der Bestellung, nichts an der Rechnung, nichts hier. Genau das ist aber der
-- Zweck der gesonderten Unternehmer-Erklärung nach § 1 Abs 1 Z 1 KSchG — eine
-- AGB-Klausel allein trägt die B2B-Beschränkung nicht, wenn faktisch jede
-- E-Mail-Adresse kaufen kann. Ein Haken, der nirgends gespeichert ist, ist im
-- Streitfall ein Haken, den es nicht gab.
--
-- EIGENE TABELLE, nicht Spalten an pending_orders:
--   • Eine Zustimmung ist ein Ereignis über eine Person zu einem Zeitpunkt.
--     Sie muss die Bestellung überleben — Storno, Rückerstattung und Löschung
--     der Order dürfen den Beweis nicht mitnehmen.
--   • Nicht jede Kauf-Bahn erzeugt eine pending_order (Abo, Budget-Aufladung).
--     order_id ist deshalb nullable, die Bahn steht immer dabei.
--
-- WARUM DER CHECK-CONSTRAINT:
-- Gespeichert wird ausschliesslich eine ERTEILTE Zustimmung. Ein Datensatz mit
-- acting_as_business = false wäre keine Zustimmung, sondern deren Gegenteil —
-- und niemand soll später eine Zeile finden, die wie ein Beleg aussieht, aber
-- keiner ist. Die Route weist unvollständige Zustimmungen vorher ab; der
-- Constraint ist die zweite Verteidigungslinie.

CREATE TABLE IF NOT EXISTS purchase_consents (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                   uuid NOT NULL REFERENCES users(id),
    tenant_id                 character varying(128) NOT NULL REFERENCES tenants(id),

    -- Welche Kauf-Bahn die Zustimmung eingeholt hat.
    lane                      text NOT NULL,

    -- Gesetzt, wo die Bahn eine Bestellung erzeugt (Pack-Käufe). Abo und
    -- Budget-Aufladung haben keine — dort bleibt es NULL.
    order_id                  uuid REFERENCES pending_orders(id),

    -- Wofür zugestimmt wurde. Redundant zur Order, aber bewusst: der Beleg
    -- soll für sich allein lesbar sein, auch wenn die Order verschwindet.
    plan_id                   character varying(128),
    quantity                  integer,
    amount_eur                numeric(10,2),

    -- Die Erklärungen selbst.
    terms_version             character varying(32) NOT NULL,
    acting_as_business        boolean NOT NULL,
    professionally_qualified  boolean NOT NULL,

    -- accepted_at kommt vom Client (Zeitpunkt des Hakens), recorded_at ist die
    -- Server-Wahrheit. Beide behalten: weichen sie stark ab, ist das selbst ein
    -- Signal.
    accepted_at               timestamptz NOT NULL,
    recorded_at               timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT purchase_consents_lane_known
        CHECK (lane IN ('subscription', 'project_pack', 'first_purchase_pack', 'topup')),
    CONSTRAINT purchase_consents_both_declarations
        CHECK (acting_as_business AND professionally_qualified)
);

CREATE INDEX IF NOT EXISTS idx_purchase_consents_user ON purchase_consents (user_id);
CREATE INDEX IF NOT EXISTS idx_purchase_consents_tenant ON purchase_consents (tenant_id);
CREATE INDEX IF NOT EXISTS idx_purchase_consents_order ON purchase_consents (order_id);

COMMENT ON TABLE purchase_consents IS
    'Beweisstueck: erteilte Kaufzustimmung (AGB + Unternehmer-Erklaerung nach '
    '§ 1 Abs 1 Z 1 KSchG) je Kaufvorgang. Nur erteilte Zustimmungen — siehe '
    'CHECK purchase_consents_both_declarations.';
