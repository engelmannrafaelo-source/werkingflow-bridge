-- 041 — seed plan row for 'report-check-credit' (needs 040 committed first;
-- Postgres refuses to use a new enum value inside the transaction that added it).
--
-- interval='project' reuses the existing manual_project_credits slot-counter
-- semantics (release_order / consume_credit) — NOT month/once. api_budget_eur=0:
-- a check-credit unlocks one document download, no KI-budget allocation.
-- price_eur=5.00 × quantity=20 (app-side) == the 100 EUR / 20 Checks pack;
-- manual_project_credits.quantity is a plain slot counter, so 20 single
-- credits are equivalent to one 100 EUR bundle without any bundle-size
-- concept in the Bridge.

INSERT INTO plans (id, app_id, name, price_eur, interval, api_budget_eur, description, is_trial, sort_order, metadata) VALUES
    ('report-check-credit', 'werking-report', 'Check-Credit', 5.00, 'project', 0,
     'Schaltet den Word-Download (Prüfbericht + korrigierte Fassung) für einen Check frei.',
     FALSE, 25, '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;
