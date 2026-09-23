-- 061 — Abbuchung idempotent je Aufruf (Partner-Berater, 2026-09-23)
--
-- Befund 23.09.2026 (dev-Bridge, Konto test-budget@test.werkingflow.com):
-- ein bezahlter Help-Agent-Lauf von werking-report steht im Ledger mit
-- 0,006598 EUR (activities 36ba57d0…, 08:22:33Z), der Monatstopf
-- report-standard blieb bei usedEur 0.0 (updated_at 2026-08-01). Der Abzug
-- selbst funktioniert — eine Handabbuchung ueber POST /v1/budget/deduct kam
-- mit 200 in 1,0 s durch. Liegen bleibt er im Worker-Pfad danach.
--
-- Der Grund, warum er dort liegen bleiben DARF: /v1/budget/deduct und
-- /v1/internal/project-budgets/deduct hatten keinen Dedup-Schluessel. Darum
-- musste der Worker jeden Abzug genau einmal und ohne Wiederholung schicken,
-- gebunden an "diese INSERT hat die Ledger-Zeile angelegt". Jeder Weg daneben
-- verlor den Abzug still:
--   * Antwort auf den Abzug verloren (Timeout 2 s)  → nie wiederholt;
--   * Antwort auf den Ledger-Schreibaufruf verloren → der Spool schreibt die
--     Zeile spaeter als 'duplicate' nach, und fuer 'duplicate' wird nie
--     abgebucht;
--   * platform-api kurz weg                        → WARNING, Ende.
--
-- Diese Tabelle ist der Schluessel. Der Worker schickt die call_uid des
-- Aufrufs (== usage_events.idempotency_key) mit; derselbe Schluessel bucht
-- hoechstens einmal ab, ein zweiter Versuch bekommt das gespeicherte Ergebnis
-- zurueck. Damit darf der Worker wiederholen und auch nach 'duplicate' buchen.
--
-- Nachweis in einer Transaktion mit dem Abzug: die Zeile wird VOR dem Abzug
-- angelegt (ein gleichzeitiger zweiter Versuch wartet am Primaerschluessel,
-- bis der erste committet oder zurueckrollt). Ein abgelehnter Abzug
-- (BUDGET_EXCEEDED, trial_expired) rollt die Zeile mit zurueck — der
-- Schluessel ist dann frei, denn es wurde nichts gebucht.
--
-- Reihenfolge beim Deploy: VOR dem Worker- und platform-api-Rollout
-- (bin/bridge-migrate.sh vor scripts/bridge-deploy.sh). Ohne Tabelle
-- antwortet ein Abzug MIT Schluessel mit 500 — laut, nicht still.

CREATE TABLE IF NOT EXISTS budget_deductions (
    idempotency_key TEXT PRIMARY KEY,
    -- 'month' (user_budgets) oder 'project' (project_budgets): derselbe
    -- Schluessel darf nicht in zwei Toepfen gelten.
    scope           TEXT           NOT NULL CHECK (scope IN ('month', 'project')),
    user_id         UUID           NOT NULL,
    plan_id         TEXT           NOT NULL,
    project_id      TEXT,
    amount_eur      NUMERIC(14, 6) NOT NULL,
    -- Antwort des ersten Abzugs; ein Wiederholer bekommt genau sie zurueck.
    result          JSONB,
    created_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS budget_deductions_user_created_idx
    ON budget_deductions (user_id, created_at);
