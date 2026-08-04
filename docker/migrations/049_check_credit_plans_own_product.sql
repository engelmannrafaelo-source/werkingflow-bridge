-- 049 — Check-Credit-Pläne gehören zum Produkt werking-check (Rafael, 2026-08-04)
--
-- Der WerkING Check ist abrechnungsmäßig ein EIGENES Produkt (Migration 048):
-- die Standalone-Käufe — die Check-Credit-Packs 1/5/20 — werden ab jetzt unter
-- app_id 'werking-check' geführt. Damit tragen neue Käufe (subscriptions,
-- pending_orders, Rechnungen, Entitlements — alle leiten app_id über
-- get_plan(plan_id).app_id ab) das richtige Produkt. Die plan_ids selbst
-- bleiben unverändert ('report-check-credit*' ist ein historischer Name, kein
-- Produktschlüssel — ein plan_id-Enum-Rename wäre reine Kosmetik mit
-- FK-Risiko).
--
-- Historische Zeilen (subscriptions/pending_orders VOR dieser Migration)
-- behalten bewusst app_id='werking-report': sie dokumentieren, unter welchem
-- Produkt der Kauf damals lief. Rechnungen werden nicht umgeschrieben.
--
-- ⚠ DEPLOY-REIHENFOLGE (je Umgebung, sonst Boot-Ausfall):
--   1. Bridge-CODE zuerst ausrollen (workers + platform-api): plans.py erlaubt
--      die Form "mehrere Projektpläne ohne Monatsplan" nicht mehr am Boot,
--      sondern resolve_billing_plan lehnt UNALLOKIERTE Calls solcher Apps
--      per Call fail-loud ab. Alter Code + diese Migration = Boot-Crash
--      (AmbiguousPlanCatalog) bei jedem Container-Restart.
--   2. DANN diese Migration anwenden.
--   3. DANN workers + platform-api neu starten (PLANS ist ein Prozess-Cache;
--      der Hot-Reload-Endpoint erreicht nur den eigenen Prozess).
--   4. DANN erst die werking-report-App-Version ausrollen, deren
--      check-attribution.ts freigeschaltete Akte als X-App-ID werking-check
--      attribuiert (Gegenstück; vorher wären allokierte Calls
--      app-mismatched → PlanResolutionError). Solange NEXT_PUBLIC_
--      CHECK_KAUF_AKTIV aus ist, ist das Fenster zwischen 2. und 4. tot.
--
-- Forward-only, idempotent.

UPDATE plans
   SET app_id = 'werking-check'
 WHERE id IN ('report-check-credit', 'report-check-credit-5', 'report-check-credit-1');
