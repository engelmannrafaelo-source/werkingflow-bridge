-- 082 — Kostendeckel je gebundenem Principal (Bridge-Owner 51c8185c, 17.11.2026; Kanzler-Entscheid (a)).
--
-- Warum nicht ueber plans/subscriptions: Ein aktiver Monatsplan fuer eine App stellt JEDEN Nutzer dieser
-- App unter das Budget-Gate (nicht lizenziert = 402 "unlicensed", src/budget/gate.py). Fuer werking-works
-- wuerde das Pyramiden-/Testkonten auf Dev und beim naechsten Prod-Deploy Prod-Nutzer treffen. Der Deckel
-- haengt deshalb am gebundenen Principal selbst und wirkt nur auf Aufrufe mit dessen Token.
--
-- proxy_cap_eur: harter Stopp nach Erschoepfung. Verbrauch = Summe usage_events (source sandbox) des
-- gebundenen Nutzers fuer die gebundene App seit Anlage des Principals. Bestandszeilen (proxy_routes = '{}')
-- bleiben unberuehrt; keine Enum-, Plan- oder Katalogaenderung.
ALTER TABLE service_principals
    ADD COLUMN IF NOT EXISTS proxy_cap_eur NUMERIC(10,2) NULL;

ALTER TABLE service_principals DROP CONSTRAINT IF EXISTS service_principals_proxy_cap;
ALTER TABLE service_principals ADD CONSTRAINT service_principals_proxy_cap CHECK (
    cardinality(proxy_routes) = 0
    OR (proxy_cap_eur IS NOT NULL AND proxy_cap_eur > 0)
) NOT VALID;
ALTER TABLE service_principals VALIDATE CONSTRAINT service_principals_proxy_cap;
