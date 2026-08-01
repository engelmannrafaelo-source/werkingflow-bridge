-- 044: Dependency deferral for ai_jobs
--
-- Motivation (2026-08-01): production must never silently fall back to a second
-- PII detector, so when the GPU privacy host is unreachable the work is DEFERRED
-- and delivered once the host returns. The existing retry machinery could not
-- express that: an executor error was TERMINAL (mark_error), and the only requeue
-- path was the watchdog's worker-death budget (GENERIC_JOB_MAX_ATTEMPTS = 3 over
-- ~4.5 minutes). The outage that motivated this lasted ~23 minutes, so a deferred
-- job would have died long before the dependency came back.
--
-- "The worker crashed" and "the dependency was not there" are different failures
-- and need different budgets, hence a SEPARATE counter rather than reusing
-- attempts:
--   attempts    — how often a runner STARTED this job (crash/requeue accounting)
--   defer_count — how many of those starts ended in a dependency wait
-- The watchdog's retry cap is evaluated on (attempts - defer_count), so waiting
-- out a long outage never consumes the crash budget, and a genuinely crash-looping
-- job is still capped exactly as before.

ALTER TABLE ai_jobs
    -- When set and in the future, the job is waiting for a dependency and MUST
    -- NOT be claimed yet. NULL = not deferred (all pre-existing rows).
    ADD COLUMN IF NOT EXISTS deferred_until TIMESTAMPTZ,
    -- How many times this job was deferred; bounds the total wait so a
    -- permanently dead dependency still fails loud instead of spinning forever.
    ADD COLUMN IF NOT EXISTS defer_count    INT NOT NULL DEFAULT 0,
    -- Last deferral reason, surfaced on GET /v1/jobs/{id} so a caller polling a
    -- long-pending job can see WHY it is waiting rather than guessing.
    ADD COLUMN IF NOT EXISTS defer_reason   TEXT;

-- The watchdog scans for claimable work every 30s; this keeps that scan from
-- degrading into a seq-scan once deferred jobs accumulate during an outage.
CREATE INDEX IF NOT EXISTS idx_ai_jobs_deferred_until
    ON ai_jobs (deferred_until)
    WHERE status IN ('pending', 'running');
