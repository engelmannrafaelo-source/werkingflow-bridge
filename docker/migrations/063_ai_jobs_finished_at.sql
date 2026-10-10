-- 063: ai_jobs.finished_at — the retention clock starts at the terminal state
--
-- Motivation (BR10R M-B, measured 10.10.2026 against a real Postgres): the
-- maintenance pass deleted every row older than 2 h counted from created_at,
-- whatever its status. A job that waited for capacity or a dependency (up to
-- ~4 h by design, registry.py DEPENDENCY_*) and then finished was deleted in the
-- same 30 s tick — the work done and booked, the result gone, the poller told
-- "unknown or expired". Rows still 'running' or parked 'pending' were deleted
-- mid-flight as well.
--
-- Retention now counts from the moment a job became terminal (done / error /
-- cancelled), and only terminal rows are deleted by age (src/jobs/store.py
-- prune_jobs). updated_at would mostly carry that moment too, but it is also
-- the column every heartbeat and progress write touches, so its meaning is
-- "last write", not "finished". finished_at is written exactly once, by the
-- terminal transition.
--
-- Rows written before this migration (and by a platform-api from before it)
-- have finished_at NULL; prune_jobs reads COALESCE(finished_at, updated_at),
-- which for a terminal row is its transition time plus at most one heartbeat
-- tail. No backfill needed, no table rewrite: nullable, no default.

ALTER TABLE ai_jobs
    ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;

-- The prune scan only ever looks at terminal rows by their retention clock.
CREATE INDEX IF NOT EXISTS idx_ai_jobs_retention
    ON ai_jobs ((COALESCE(finished_at, updated_at)))
    WHERE status IN ('done', 'error', 'cancelled');
