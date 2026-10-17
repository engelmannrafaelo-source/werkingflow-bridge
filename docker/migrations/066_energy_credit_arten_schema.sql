-- 066 — Energy-Credit-Arten, Teil 1: Enum-Werte und Katalogspalten.
--
-- Rafael-Entscheid 17.10.2026 (e-energy-preis-wizard-20261017 = a): Fuer
-- WerkING Energy gilt die Staffel der Website — 150 / 300 / 600 EUR netto je
-- Berichtsart. Statt einer Credit-Art ('energy-project') gibt es drei. Die
-- Zuordnung Berichtsart -> plan_id gehoert in die Energy-App; die Bridge kennt
-- nur Plaene.
--
-- Split in eine EIGENE Migration: ALTER TYPE ... ADD VALUE darf nicht in der
-- Transaktion verwendet werden, die es hinzugefuegt hat (Muster 040/041,
-- 042/043). Die Zeilen folgen in 067.
--
-- Neue Spalten (alle mit Default, der den Ist-Zustand jeder Bestandszeile
-- genau beschreibt — alter Code liest sie nicht und bleibt unberuehrt):
--
--   price_basis    'net' | 'gross'. Jeder Katalogpreis ist heute netto: die
--                  Rechnung (billing_service._create_invoice) schlaegt die USt
--                  auf price_eur auf. Die Spalte macht das sichtbar statt
--                  implizit; Default 'net' beschreibt also den Bestand.
--   sellable       FALSE = darf nicht mehr gekauft werden (Checkout und
--                  Bestellung lehnen ab), bleibt aber im Katalog, damit
--                  Bestandsguthaben, Budgets und Abos daran weiterlaufen.
--                  Anders als is_active, das den Plan aus dem Laufzeitkatalog
--                  nimmt (dann scheitert get_plan fuer jede Bestandszeile).
--   budget_plan_id NULL = der Plan ist selbst ein Budget-Topf (Bestand).
--                  Gesetzt = reine Credit-Art: ein verbrauchter Slot allokiert
--                  sein KI-Budget unter DIESEM Topf-Plan. Die Topf-Aufloesung
--                  (plan_resolution) sieht dann pro App weiter genau einen
--                  Projekt-Topf; Credits dieses Topf-Plans gelten fuer jede
--                  Credit-Art, die auf ihn zeigt (Migrationsweg Bestandsguthaben,
--                  siehe 067).
--
-- Forward-only, idempotent.

ALTER TYPE plan_id ADD VALUE IF NOT EXISTS 'energy-anlagendokumentation';
ALTER TYPE plan_id ADD VALUE IF NOT EXISTS 'energy-objektbericht';
ALTER TYPE plan_id ADD VALUE IF NOT EXISTS 'energy-analysebericht';

ALTER TABLE plans
  ADD COLUMN IF NOT EXISTS price_basis VARCHAR(8) NOT NULL DEFAULT 'net'
  CONSTRAINT plans_price_basis_chk CHECK (price_basis IN ('net', 'gross'));

ALTER TABLE plans
  ADD COLUMN IF NOT EXISTS sellable BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE plans
  ADD COLUMN IF NOT EXISTS budget_plan_id plan_id NULL REFERENCES plans(id);

ALTER TABLE plans DROP CONSTRAINT IF EXISTS plans_budget_plan_not_self_chk;
ALTER TABLE plans
  ADD CONSTRAINT plans_budget_plan_not_self_chk
  CHECK (budget_plan_id IS NULL OR budget_plan_id <> id);
