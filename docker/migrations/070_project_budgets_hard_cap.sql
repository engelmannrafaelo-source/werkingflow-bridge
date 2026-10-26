-- 070_project_budgets_hard_cap.sql
--
-- Lauf-Deckel: ein Projektbudget kann ausdruecklich als harte Obergrenze
-- markiert werden (hard_cap = true). Fuer solche Budgets fragen evaluate() und
-- deduct() das TopUp nie an (Befund Muehl 25.10.2026, 200-EUR-Limit). Alle
-- anderen Projektbudgets (Report-Check-Credits, KFR) behalten den TopUp-
-- Rueckgriff nach BUDGET-MODELL.md Regel 6 unveraendert.
--
-- Forward-only, idempotent. Bestehende Zeilen bleiben false; ein laufender
-- Kundenlauf wird bewusst per UPDATE markiert, nicht durch diese Migration.

ALTER TABLE project_budgets
    ADD COLUMN IF NOT EXISTS hard_cap BOOLEAN NOT NULL DEFAULT FALSE;
