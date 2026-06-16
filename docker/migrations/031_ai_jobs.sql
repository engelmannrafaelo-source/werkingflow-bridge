-- 031_ai_jobs.sql
-- Generic async-job store for the Bridge. See bridge-async-jobs-spec.md.
--
-- Why this table
-- --------------
-- Today only /v1/research runs async (dispatch → poll), and its job state lives
-- as JSON files on a shared volume (main.py:_save_research_job). That file store
-- is multi-worker-visible (shared named volume) but has two limits the generic
-- "every AI call is a durable job" goal cannot accept:
--   1. A container restart LOSES in-flight 'running' jobs (the asyncio task dies,
--      the file is orphaned at 'running' until TTL). No requeue possible.
--   2. No atomic claim / heartbeat → no safe cross-worker requeue of stale jobs.
--
-- Postgres gives atomic status transitions, a heartbeat for liveness, and an
-- attempts counter for an idempotent, capped requeue — i.e. a job actually
-- survives a worker death ("kommt aufs Haupt"). The Bridge already runs Postgres
-- (billing/usage), so this adds no new dependency.
--
-- Generic over call-type: `kind` selects the executor (research|chat|pdf|…),
-- `payload` is the unchanged request body of that call-type. `payload_digest`
-- (sha256) lets a requeue / accidental double-dispatch be deduplicated instead
-- of paid twice.
--
-- NOTE on PII: `payload`/`result` may contain user content (same as today's
-- research job files, which store query+result in plaintext). State is internal
-- and TTL-pruned; at-rest encryption is a documented future hardening, not in
-- this migration. Forward-only, idempotent.

CREATE TABLE IF NOT EXISTS ai_jobs (
    job_id          VARCHAR        PRIMARY KEY,
    kind            VARCHAR        NOT NULL,
    -- pending → running → done | error
    status          VARCHAR        NOT NULL DEFAULT 'pending',
    -- Unchanged request body of the call-type (kind-specific). Needed for requeue.
    payload         JSONB,
    -- sha256 of the canonical payload — idempotency / double-dispatch guard.
    payload_digest  VARCHAR,
    -- app_id / agent_id / workflow_id / tenant_id / user_id, persisted so the
    -- background runner bills/attributes correctly even after the HTTP request
    -- that started the job is long gone.
    attribution     JSONB,
    -- { phase, percent, partial } — updated incrementally for progress/streaming.
    progress        JSONB,
    -- kind-specific result, only when status='done'.
    result          JSONB,
    -- { message, code } only when status='error'.
    error           JSONB,
    -- requeue accounting: number of times the runner has started this job.
    attempts        INT            NOT NULL DEFAULT 0,
    -- liveness signal from the active runner; a 'running' job whose heartbeat is
    -- older than the stale window is a candidate for requeue (capped by attempts).
    heartbeat_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

-- Watchdog scans by status; cleanup scans by age.
CREATE INDEX IF NOT EXISTS idx_ai_jobs_status  ON ai_jobs (status);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_created ON ai_jobs (created_at);
