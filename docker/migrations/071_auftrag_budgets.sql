-- 071_auftrag_budgets.sql
--
-- Auftragsbudget: ein interner Kostendeckel je Auftrag (eigene workflow_id als
-- project_id, frei vorgegebenes limit_eur, hard_cap = true) als ganz normale
-- Zeile in project_budgets. Gate und Abzug bleiben unveraendert, sie finden die
-- Zeile ueber (user_id, project_id). Entwurf ENTWURF-AUFTRAGSBUDGET-8462d3f6-
-- 20261027.md, Form B: die Auftragszeile traegt einen Elternverweis auf das
-- Kundenprojekt, die Projektsumme wird beim Lesen gebildet, die Kundenliste
-- (list_budgets) zeigt nur kind = 'projekt'.
--
-- project_budget_events protokolliert jedes Anlegen und Anheben (alt, neu,
-- Grund, Aufrufer). Der Teil-Unique-Index macht das Anheben ueber einen
-- Idempotenzschluessel wiederholbar: ein zweiter Aufruf mit demselben
-- Schluessel findet die Zeile und hebt nicht noch einmal an.
--
-- Forward-only, idempotent. Bestehende Zeilen bekommen kind = 'projekt' ueber
-- den konstanten Default (kein Neuschreiben der Tabelle unter Postgres 16);
-- der laufende Code liest die neuen Spalten nicht und bleibt lauffaehig.

ALTER TABLE project_budgets
    ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'projekt',
    ADD COLUMN IF NOT EXISTS parent_project_id VARCHAR;

-- kind-Pruefung (idempotent: ADD CONSTRAINT kennt kein IF NOT EXISTS).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'project_budgets_kind_valid') THEN
    ALTER TABLE project_budgets
        ADD CONSTRAINT project_budgets_kind_valid CHECK (kind IN ('projekt', 'auftrag'));
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS project_budget_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID            NOT NULL,
    plan_id         VARCHAR         NOT NULL,
    project_id      VARCHAR         NOT NULL,
    event           VARCHAR         NOT NULL,           -- 'created' | 'raised'
    old_limit_eur   NUMERIC(12, 4),
    new_limit_eur   NUMERIC(12, 4)  NOT NULL,
    grund           VARCHAR         NOT NULL,
    actor           VARCHAR         NOT NULL,           -- X-App-ID/X-Agent-ID des Aufrufers, sonst 'service'
    idempotency_key VARCHAR,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Idempotenzschluessel je (user, plan, project): nur gesetzte Schluessel
-- sind eindeutig, 'created'-Zeilen ohne Schluessel kollidieren nicht.
CREATE UNIQUE INDEX IF NOT EXISTS ux_project_budget_events_key
    ON project_budget_events (user_id, plan_id, project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
