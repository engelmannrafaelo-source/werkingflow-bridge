-- 063 — Abbruchwunsch fuer laufende Jobs (BR10b S1, Haus Dev, 2026-10-10)
--
-- DELETE /v1/jobs/{id} bricht nur 'pending' ab; ein 'running' Job bekommt 409.
-- Stellt sich dieser Job danach selbst zurueck (defer_job: running -> pending,
-- z. B. 429 im Selbstaufruf) oder stirbt sein Worker, wuerde ihn der Watchdog
-- spaeter erneut starten — und er wuerde gebucht, obwohl sein Eigentuemer ihn
-- schon aufgegeben hat. Der Aufrufer schickt genau ein DELETE und kann das
-- nicht auffangen.
--
-- Diese Spalte merkt sich den Wunsch: gesetzt von store.cancel_job, wenn der
-- Job gerade laeuft. defer_job setzt dann 'cancelled' statt 'pending', und der
-- Watchdog nimmt eine solche Zeile nie wieder auf, sondern schliesst sie als
-- 'cancelled'. Ein Lauf, der zu Ende kommt, endet normal (done/error).
--
-- Rein additiv, nullable, ohne Default: kein Tabellen-Rewrite, alter Code
-- laeuft gegen das neue Schema unveraendert weiter. Muss VOR dem Code
-- angewendet sein (das Migrations-Tor in bridge-deploy.sh Phase 3.7 erzwingt
-- das), denn defer_job und claim_stale_job lesen die Spalte.

ALTER TABLE ai_jobs
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;
