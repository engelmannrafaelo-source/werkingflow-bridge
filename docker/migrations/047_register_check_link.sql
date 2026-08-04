-- 047 — Selbstregistrierung: Akt-Verknüpfung persistieren (Rafael, 2026-08-04)
--
-- /v1/auth/register bekommt vom Check-Trichter eine checkId mitgereicht und
-- hat sie bisher NUR GELOGGT (identity/routes.py). Damit war die Verbindung
-- "dieser Akt hat X EUR gekostet" → "dieser Kunde hat X EUR Akquise gekostet"
-- nicht herstellbar, obwohl beide Hälften einzeln vorliegen (usage_events
-- trägt die checkId als workflow_id/project_id; users trägt den Kunden).
--
-- Datensparsamkeit bleibt gewahrt: die Akt-ID ist ein Zufallstoken ohne
-- Personenbezug. Die Verbindung Akt→Person entsteht ausschließlich dadurch,
-- dass der Kunde sich SELBST mit diesem Akt registriert — sie ist seine
-- freiwillige Angabe, kein Tracking. Anonyme Besucher bleiben unverknüpft.
--
-- Der zweite Teil derselben Entscheidung (Bedrock-Pin für Selbstregistrierte)
-- braucht kein Schema: users.provider_config existiert seit dem
-- Operator-Pin-Modell und wird von der Register-Route befüllt.

ALTER TABLE users ADD COLUMN IF NOT EXISTS registered_from_check_id TEXT;

COMMENT ON COLUMN users.registered_from_check_id IS
  'Akt-ID (checkId) aus der Selbstregistrierung über den Check-Trichter. '
  'Verknüpft Akquisekosten (usage_events.workflow_id) mit dem Kunden. '
  'NULL für alle anderen Entstehungswege.';
