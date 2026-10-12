-- 065 — Idempotency-Key fuer POST /v1/jobs (BRREF2, Haus Dev, 2026-10-12)
--
-- Ein Aufrufer, dessen Antwort auf POST /v1/jobs verloren geht (Timeout,
-- Verbindungsabbruch, eigener Neustart), schickt denselben Auftrag noch einmal
-- und bekam bisher einen ZWEITEN Job — doppelte Arbeit, doppelt gebucht. Mit
-- dem Header Idempotency-Key wird daraus genau ein Job je (Eigentuemer,
-- Schluessel):
--
--   idempotency_owner  wer den Schluessel vergeben hat — Principal bzw.
--                      gemeinsamer Schluessel plus App (src/jobs/routes.py
--                      _idempotency_owner). Zwei Apps mit demselben
--                      Schluessel sind zwei verschiedene Auftraege.
--   idempotency_key    der Header, ASCII, hoechstens 200 Zeichen.
--
-- Die Eindeutigkeit traegt der UNIQUE-Index, nicht ein Lesen-dann-Schreiben:
-- zwei gleichzeitige POSTs laufen beide in denselben INSERT ... ON CONFLICT DO
-- NOTHING, genau einer legt die Zeile an, der andere liest sie danach
-- (src/jobs/store.py create_job_idempotent). Partiell auf Zeilen MIT Schluessel,
-- damit Jobs ohne Header (der Normalfall) weder Index noch Verhalten aendern.
--
-- Aufbewahrung: der Schluessel lebt so lange wie seine Job-Zeile. Nicht-
-- terminale Zeilen loescht die Aufbewahrung nie, also ueberlebt der Schluessel
-- jede Laufzeit. Terminale Zeilen MIT Schluessel loescht prune_jobs erst
-- 24 h nach dem Endzustand statt nach den ueblichen 2 h
-- (IDEMPOTENCY_RETENTION_SECONDS in store.py).
--
-- Rein additiv, nullable, ohne Default: kein Tabellen-Rewrite, alter Code
-- laeuft gegen das neue Schema unveraendert. Muss VOR dem Code angewendet sein
-- (Migrations-Tor in bridge-deploy.sh Phase 3.7): create_job_idempotent und
-- prune_jobs lesen die Spalten.

ALTER TABLE ai_jobs
    ADD COLUMN IF NOT EXISTS idempotency_owner TEXT,
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_jobs_idempotency
    ON ai_jobs (idempotency_owner, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
