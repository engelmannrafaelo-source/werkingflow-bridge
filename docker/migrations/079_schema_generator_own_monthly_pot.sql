-- 079: Schema-Generator bekommt auf der Prod-Bridge einen EIGENEN internen Monatstopf (Option A, T-SCHEMA-PROD-BRIDGE, 16.11.2026).
--
-- Warum: 076 legt 'schema-generator' als inaktiven Alias auf 'report-standard' an (api 0). Der Monats-Alias wird erst ab
-- c73175b ausgewertet; der Prod-Code (daabf89-Linie) kennt ihn nicht, und der Katalog laedt nur aktive Zeilen
-- (src/budget/plan_repo.py). Ohne diese Migration antwortet das strict_monthly-Gate fuer werking-schema-generator 503.
--
-- Was: NUR wenn die Zeile exakt im 076-Zustand steht UND noch keine Subscription fuer die App existiert:
--   is_active TRUE, api_budget_eur 100.00, budget_plan_id NULL. Preis 0, sellable FALSE, is_trial FALSE bleiben.
-- Jeder andere Zustand (z. B. Dev: Zeile schon aktiv als Alias) ist ein bewusster No-op, damit Dev nicht still umgebaut wird.
-- Fehlt die Zeile (076 nicht gelaufen), bricht die Migration laut ab.
DO $$
DECLARE r plans%ROWTYPE; subs integer;
BEGIN
  SELECT * INTO r FROM plans WHERE id = 'schema-generator' FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION '079: plan schema-generator fehlt (076 zuerst)';
  END IF;
  IF r.is_active IS NOT FALSE OR r.budget_plan_id IS DISTINCT FROM 'report-standard'
     OR r.api_budget_eur <> 0 OR r.price_eur <> 0 OR r.sellable IS NOT FALSE OR r.is_trial IS NOT FALSE
     OR r.app_id <> 'werking-schema-generator' OR r.interval <> 'month' THEN
    RAISE NOTICE '079: schema-generator nicht im 076-Ausgangszustand, keine Aenderung';
    RETURN;
  END IF;
  SELECT count(*) INTO subs FROM subscriptions WHERE app_id = 'werking-schema-generator';
  IF subs <> 0 THEN
    RAISE EXCEPTION '079: % Subscription(s) fuer werking-schema-generator vorhanden, Umbau verweigert', subs;
  END IF;
  UPDATE plans
     SET is_active = TRUE, api_budget_eur = 100.00, budget_plan_id = NULL,
         description = 'Interner Monatstopf oeffentlicher Schema-Generator (werking.tools), 100 EUR/Monat, nicht verkaeuflich'
   WHERE id = 'schema-generator';
END $$;
