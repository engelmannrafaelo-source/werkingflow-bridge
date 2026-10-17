-- 067 — Energy-Credit-Arten, Teil 2: Katalogzeilen (braucht 066 committed).
--
-- Staffel laut Website (werkingflow-production apps/werking-tools/src/lib/
-- products.ts, Energy-pricing) und PRICING-STRATEGY.md, Abschnitt
-- "WerkING Energy — dreistufig". Rafael-Entscheid 17.10.2026:
--
--   energy-anlagendokumentation  150 EUR netto  (Berichtsart anlagendokumentation)
--   energy-objektbericht         300 EUR netto  (Berichtsart objektbericht)
--   energy-analysebericht        600 EUR netto  (Berichtsart analysebericht)
--
-- Alle drei sind reine Credit-Arten (budget_plan_id = 'energy-project'): ein
-- verbrauchter Slot allokiert sein KI-Budget (100 EUR, wie bisher je Projekt)
-- unter dem Topf 'energy-project'. Damit bleiben Topf-Aufloesung, Lazy-
-- Allokation, Budgetanzeige und Projekt-Resets unveraendert.
--
-- 'energy-project' (die bisherige einzige Credit-Art):
--   * bleibt aktiv — Bestandsguthaben, allokierte Projektbudgets und Abos
--     haengen an ihm;
--   * sellable = FALSE — nicht mehr kaeuflich, verkauft werden nur noch die
--     drei Arten;
--   * price_eur = 600 — gleicht die Startdaten an den Live-Stand an (020 hatte
--     1000 geseedet, live stand per Hand-UPDATE 600). Der Preis ist ohne
--     Verkauf nur noch Auskunft.
--
-- MIGRATIONSWEG BESTANDSGUTHABEN: Es werden KEINE manual_project_credits-
-- Zeilen umgeschrieben. Ein nicht verbrauchter 'energy-project'-Slot gilt fuer
-- JEDE der drei Berichtsarten (project_credits_service.consume_credit: erst
-- eigene Art, dann der Topf-Plan). Begruendung: der Slot wurde zu 600 oder
-- 1000 EUR bezahlt, also mindestens zum Preis der teuersten Art. Eine feste
-- Umbuchung auf eine Art haette dem Kunden Nutzbarkeit genommen.
--
-- ⚠ DEPLOY-REIHENFOLGE (je Umgebung, Muster 049): 066 + 067 anwenden, dann
-- SOFORT den Bridge-Code dieser Aenderung ausrollen (workers + platform-api).
-- Laufende alte Prozesse halten ihren PLANS-Cache und sehen die Zeilen nicht.
-- Ein ALTER Prozess, der dazwischen neu startet, saehe vier Energy-
-- Projektplaene ohne budget_plan_id-Logik und lehnte unallokierte Energy-
-- Calls mit PlanResolutionError ab. Erst danach die Energy-App ausrollen.
--
-- metadata.refundGuarantee wie bei 'energy-project' (020) — dieselbe Zusage je
-- Projekt, nur nach Berichtsart bepreist.
--
-- Forward-only, idempotent (Wiederholung setzt dieselben Werte).

INSERT INTO plans (id, app_id, name, price_eur, interval, api_budget_eur, description,
                   is_trial, sort_order, metadata, price_basis, sellable, budget_plan_id) VALUES
    ('energy-anlagendokumentation', 'werking-energy', 'WerkING Energy — Anlagendokumentation',
     150, 'project', 100,
     'Ein Projekt-Credit für eine Anlagendokumentation. KI-Budget inklusive.',
     FALSE, 31, '{"refundGuarantee": {"days": 14, "until": "pdf-export"}}'::jsonb, 'net', TRUE, 'energy-project'),
    ('energy-objektbericht', 'werking-energy', 'WerkING Energy — Objektbericht',
     300, 'project', 100,
     'Ein Projekt-Credit für einen Objektbericht. KI-Budget inklusive.',
     FALSE, 32, '{"refundGuarantee": {"days": 14, "until": "pdf-export"}}'::jsonb, 'net', TRUE, 'energy-project'),
    ('energy-analysebericht', 'werking-energy', 'WerkING Energy — Analysebericht',
     600, 'project', 100,
     'Ein Projekt-Credit für einen Analysebericht. KI-Budget inklusive.',
     FALSE, 33, '{"refundGuarantee": {"days": 14, "until": "pdf-export"}}'::jsonb, 'net', TRUE, 'energy-project')
-- DO UPDATE statt DO NOTHING (Zweitpruefung 17.10.2026, R2): Preis,
-- Netto-Kennzeichen, Kaeuflichkeit und Topf-Zuordnung SIND der Rafael-Entscheid
-- bzw. die Bedingung fuer genau EINEN Energy-Topf. Eine vorhandene, falsch
-- angelegte Zeile (z. B. von Hand, 999 brutto, ohne budget_plan_id) bliebe
-- sonst still falsch — ohne budget_plan_id waere sie ein zweiter Projekt-Topf
-- und unallokierte Energy-Calls scheiterten mit PlanResolutionError. Name,
-- Beschreibung und Budget bleiben unberuehrt; die Seeds 020/041/043 nutzen
-- DO NOTHING, weil sie Startdaten sind, keine Preisentscheidung.
ON CONFLICT (id) DO UPDATE
   SET price_eur      = EXCLUDED.price_eur,
       price_basis    = EXCLUDED.price_basis,
       sellable       = EXCLUDED.sellable,
       budget_plan_id = EXCLUDED.budget_plan_id,
       updated_at     = NOW();

UPDATE plans
   SET sellable = FALSE,
       price_eur = 600,
       updated_at = NOW()
 WHERE id = 'energy-project';
