-- 045: Report-Sitz API-Budget EUR 50 -> EUR 100.
--
-- Rafael-Entscheid 01.08.2026 (SSoT: werkingflow-business/shared/operations/
-- PRICING-STRATEGY.md). Basis: Prod-Verbrauchsdaten 07/2026 — Median-Nutzer
-- EUR 4–40/Monat, Power-User EUR 107; das Budget ist ein Verbrauchs-Limit zu
-- Listenpreisen, kein Kostenposten.
--
-- Zwei Schritte:
--   1. Plan-Katalog: report-standard bekommt api_budget_eur = 100. Wirkt für
--      alle künftigen Provisionierungen. Nach Anwendung Hot-Reload nötig:
--      POST /v1/billing/plans/reload (der PLANS-Cache liest die Tabelle neu).
--   2. Bestandssitze: user_budgets-Einträge tragen einen limitEur-Snapshot
--      aus der Provisionierung. Es gibt keinen Rollover-Job, der ihn vom Plan
--      nachzieht — ohne dieses UPDATE blieben Bestandskunden dauerhaft auf 50.
--      Guard auf =50, damit individuell gesetzte Limits unangetastet bleiben.
--      usedEur/resetAt bleiben unverändert (keine Verbrauchs-Rücksetzung).

UPDATE plans
SET api_budget_eur = 100
WHERE id = 'report-standard';

UPDATE user_budgets
SET monthly_budgets = jsonb_set(monthly_budgets, '{report-standard,limitEur}', '100'::jsonb),
    updated_at = NOW()
WHERE monthly_budgets ? 'report-standard'
  AND (monthly_budgets -> 'report-standard' ->> 'limitEur')::numeric = 50;
