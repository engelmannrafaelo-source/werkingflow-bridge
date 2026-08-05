-- 052 — Check-Credit-Plaene kundenfaehig benennen (Rafael, 2026-08-05)
--
-- Der Plan-NAME ist Kundentext: er steht auf der Mollie-Checkout-Seite
-- ("Check-Credit Einzelkauf × 1"), in der Rechnungs-Position (seit dem
-- pending_orders-Fix ohne internen plan_id-Zusatz) und im Portal-Katalog.
-- Bisher fehlte die Produktmarke voellig — der Kunde kauft "WerkING Check",
-- nicht "Check-Credit". "Guthaben" statt "Pruefungen", solange die
-- Quota-Kopplung (Credits vs. Lauf-Kontingent) nicht entschieden ist.
--
-- Namen sind plans-Tabellen-Daten (hot-reloadbar via POST
-- /v1/billing/plans/reload bzw. Container-Restart). Forward-only, idempotent.

UPDATE plans SET name = 'WerkING Check — Guthaben Einzelkauf'  WHERE id = 'report-check-credit-1';
UPDATE plans SET name = 'WerkING Check — Guthaben 5er-Paket'   WHERE id = 'report-check-credit-5';
UPDATE plans SET name = 'WerkING Check — Guthaben 20er-Paket'  WHERE id = 'report-check-credit';
