-- Deploy alias-aware platform/workers BEFORE this catalog row is activated.
-- A logical subscription with NO new allowance or checkout. The existing
-- account's report-standard pot retains limit/usage/reset (1000 != catalog100).
-- No subscriptions, principals, users, counters or budget rows are fabricated.
INSERT INTO plans (id, app_id, name, price_eur, interval, api_budget_eur,
                   description, is_trial, is_active, price_basis, sellable, budget_plan_id)
VALUES ('schema-generator', 'werking-schema-generator', 'WerkIng Schema-Generator',
        0, 'month', 0, 'Interner Akquiseablauf; bestehender Check-/Report-Monatstopf',
        FALSE, TRUE, 'net', FALSE, 'report-standard')
ON CONFLICT (id) DO NOTHING;
