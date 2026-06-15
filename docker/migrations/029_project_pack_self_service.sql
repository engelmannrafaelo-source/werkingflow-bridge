-- 029_project_pack_self_service.sql
-- Self-Service-Nachbestellung von Projekt-Paketen (z.B. Energy) via Mollie-Einmalzahlung.
--
-- Heute laufen Projekt-Pakete nur über die manuelle Rechnungs-Lane
-- (pending_orders + Operator-Freigabe via release_order). Dieses Feature
-- erlaubt BESTANDSKUNDEN, weitere Projekt-Pakete self-service per
-- Mollie-Einmalzahlung zu kaufen. Der Mollie-Webhook ruft DIESELBE
-- release_order()-Logik wie der Operator → manual_project_credits.
-- (Erstakquise bleibt bewusst manuell/partner-geführt.)
--
-- 1) Neuer pending_payment_type 'project_pack' für den Webhook-Dispatch.
-- 2) pending_orders.payment_method: trennt die manuelle Lane (Mahn-Email,
--    Operator-Freigabe) vom Mollie-Self-Service-Pfad (keine Mahn-Email,
--    Auto-Release per Webhook).
--
-- Forward-only, idempotent.

ALTER TYPE pending_payment_type ADD VALUE IF NOT EXISTS 'project_pack';

ALTER TABLE pending_orders
  ADD COLUMN IF NOT EXISTS payment_method VARCHAR(16) NOT NULL DEFAULT 'manual'
  CONSTRAINT pending_orders_payment_method_chk
    CHECK (payment_method IN ('manual', 'mollie'));
