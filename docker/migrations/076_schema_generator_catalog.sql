-- Deploy alias-aware platform/workers BEFORE this catalog row is activated.
-- Row is inserted INACTIVE (is_active=FALSE): reload_plans() reads only active rows,
-- so processes still running pre-alias code (also after an auto-rollback) keep booting.
-- Activation is a deliberate later step once alias-aware code runs everywhere
-- and the principal exists:  UPDATE plans SET is_active = TRUE WHERE id = 'schema-generator';
-- A logical subscription with NO new allowance or checkout. The existing
-- account's report-standard pot retains limit/usage/reset (1000 != catalog100).
-- No subscriptions, principals, users, counters or budget rows are fabricated.
INSERT INTO plans (id, app_id, name, price_eur, interval, api_budget_eur,
                   description, is_trial, is_active, price_basis, sellable, budget_plan_id)
VALUES ('schema-generator', 'werking-schema-generator', 'WerkIng Schema-Generator',
        0, 'month', 0, 'Interner Akquiseablauf; bestehender Check-/Report-Monatstopf',
        FALSE, FALSE, 'net', FALSE, 'report-standard')
ON CONFLICT (id) DO NOTHING;
