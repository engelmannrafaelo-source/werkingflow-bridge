-- 043 — seed plan rows for the Check-Credit price tiers (needs 042 committed
-- first; Postgres refuses to use a new enum value inside the transaction that
-- added it).
--
-- Semantik identisch zu 041: interval='project' (manual_project_credits
-- Slot-Counter), api_budget_eur=0 (ein Credit schaltet einen Download frei,
-- kein KI-Budget). Der Rabatt steckt ausschließlich im Stückpreis; die App
-- ruft jede Stufe mit ihrer festen Menge auf (Guard in billing_service:
-- FIRST_PURCHASE_PACK_FIXED_QUANTITIES verhindert Stückpreis-Arbitrage,
-- z.B. quantity=1 auf dem 5-EUR-Plan des 20er-Pakets).

INSERT INTO plans (id, app_id, name, price_eur, interval, api_budget_eur, description, is_trial, sort_order, metadata) VALUES
    ('report-check-credit-5', 'werking-report', 'Check-Credit 5er-Paket', 7.00, 'project', 0,
     'Schaltet den Word-Download (Prüfbericht + korrigierte Fassung) für einen Check frei — 5er-Paket, 7 EUR je Check.',
     FALSE, 26, '{}'::jsonb),
    ('report-check-credit-1', 'werking-report', 'Check-Credit Einzelkauf', 9.00, 'project', 0,
     'Schaltet den Word-Download (Prüfbericht + korrigierte Fassung) für einen Check frei — Einzelkauf.',
     FALSE, 27, '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;
