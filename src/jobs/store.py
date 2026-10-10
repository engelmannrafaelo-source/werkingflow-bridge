"""Postgres-backed durable store for generic async jobs (table: ai_jobs).

Since ADR-0009 Schritt 2d this is platform-api's module: workers reach it
through /v1/internal/jobs* (src/internal_routes.py) via src.jobs.store_client,
and only fall back to calling these functions directly while they still carry
BRIDGE_DB_URL. The status constants below are shared by both stages.

No dependency on main.py / app — pure data access, so it is unit-testable and
import-safe. All functions assume the asyncpg pool is initialized
(src.db.client.init_pool, called in the process lifespan when BRIDGE_DB_URL is
set); callers gate before reaching here (store_client.is_store_available).

JSONB columns are written with an explicit ::jsonb cast on a json.dumps string
and read back with _loads (asyncpg returns jsonb as text by default).
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.db.client import get_pool

# Terminal vs in-flight. Polling clients stop on done/error/cancelled.
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_DONE = "done"
JOB_STATUS_ERROR = "error"
# BR10: the owner withdrew the job before any worker started it. Reached from
# 'pending' (cancel_job), and — BR10b — from a run its owner asked to cancel
# while it was 'running' (cancel_requested_at) once that run parks itself
# (defer_job) or its worker dies (claim_stale_job). Nothing leaves it: every
# claim path selects 'pending'/'running' only, so a cancelled row is never
# started again and never billed again. Every transition into it stamps
# finished_at, and retention removes it like any other terminal row.
JOB_STATUS_CANCELLED = "cancelled"
JOB_TERMINAL_STATUSES = (JOB_STATUS_DONE, JOB_STATUS_ERROR, JOB_STATUS_CANCELLED)

# Retention (BR11). Only terminal rows may be deleted by age, and their age
# counts from the terminal transition (finished_at, migration 063), never from
# created_at. A row that is still pending or running is never deleted by
# retention: its bounds are find_abandoned (crash budget) and defer_count (wait
# budget), plus the loud max-age backstop in prune_jobs.
_TERMINAL_SQL = "('done', 'error', 'cancelled')"
_RETENTION_CLOCK = "COALESCE(finished_at, updated_at)"


class JobRowMissing(LookupError):
    """A write that must land on an existing job row found none.

    Before BR11 mark_done/mark_error/heartbeat were plain UPDATEs whose row
    count nobody looked at: when the row had been deleted underneath a running
    job (the old created_at retention did exactly that), the result was
    written into nothing and the call returned None. The work was done and
    paid for, the result was gone, and nobody was told. Raised instead."""

    def __init__(self, op: str, job_id: str):
        super().__init__(f"job store {op}: no row for job {job_id} (deleted or never created)")
        self.op = op
        self.job_id = job_id


def payload_digest(payload: Optional[Dict[str, Any]]) -> str:
    """Stable sha256 of the canonical payload — idempotency / double-dispatch guard."""
    canonical = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _loads(value: Any) -> Any:
    """asyncpg returns jsonb as str unless a codec is set — decode defensively."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _row_to_job(row) -> Dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "kind": row["kind"],
        "status": row["status"],
        "payload": _loads(row["payload"]),
        "payload_digest": row["payload_digest"],
        "attribution": _loads(row["attribution"]),
        "progress": _loads(row["progress"]),
        "result": _loads(row["result"]),
        "error": _loads(row["error"]),
        "attempts": row["attempts"],
        "heartbeat_at": row["heartbeat_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        # Dependency-deferral columns (migration 044). Read defensively ONLY for
        # the rollout window in which the new image can be live for a moment
        # before the migration has been applied: without this, every job read
        # would KeyError. Absent column → "never deferred", which is exactly the
        # pre-044 behaviour, so nothing silently changes semantics.
        "deferred_until": _col(row, "deferred_until"),
        "defer_count": _col(row, "defer_count", 0),
        "defer_reason": _col(row, "defer_reason"),
        # Migration 063 — same rollout-window reasoning as above.
        "finished_at": _col(row, "finished_at"),
    }


def _col(row, name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError):
        return default


async def create_job(
    job_id: str,
    kind: str,
    payload: Optional[Dict[str, Any]],
    attribution: Optional[Dict[str, Any]],
) -> None:
    """Insert a fresh job at status='pending'. Idempotent on job_id (no-op on conflict)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ai_jobs (job_id, kind, status, payload, payload_digest, attribution)
            VALUES ($1, $2, 'pending', $3::jsonb, $4, $5::jsonb)
            ON CONFLICT (job_id) DO NOTHING
            """,
            job_id,
            kind,
            json.dumps(payload, default=str) if payload is not None else None,
            payload_digest(payload),
            json.dumps(attribution, default=str) if attribution is not None else None,
        )


async def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM ai_jobs WHERE job_id = $1", job_id)
    return _row_to_job(row) if row else None


async def mark_running(job_id: str) -> bool:
    """Claim a FRESH job for the dispatching worker: pending → running, bump
    attempts, stamp heartbeat. Called by the runner.

    Returns False when the row was not 'pending' any more — cancelled by its
    owner in the window between submit and dispatch (BR10), or already claimed
    by the watchdog. The caller must then NOT run the job. The status condition
    is what makes cancel_job and this claim mutually exclusive: both write the
    same row, Postgres serialises them on its row lock, and whichever commits
    second finds the status it requires gone."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE ai_jobs
               SET status = 'running', attempts = attempts + 1,
                   heartbeat_at = NOW(), updated_at = NOW()
             WHERE job_id = $1 AND status = 'pending'
            """,
            job_id,
        )
    return _affected(result) == 1


def _affected(result: Any) -> int:
    """Row count from an asyncpg command tag ("UPDATE 1"). A tag that does not
    parse is a driver contract break — raise, never read it as 0 rows."""
    try:
        return int(str(result).split()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(f"unparseable command tag from Postgres: {result!r}") from None


async def cancel_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Withdraw a job that no worker has started (BR10). Atomic.

    Returns None for an unknown id, else ``{"status", "changed",
    "cancel_requested"}``: the status the row has after this call, whether
    THIS call moved it, and whether a cancel wish is now recorded on a
    running row. Only 'pending' (fresh or deferred — deferral keeps the status
    'pending') becomes 'cancelled'; every other status is returned unchanged
    for the caller to classify. ``FOR UPDATE`` holds the row lock across the
    read and the write, so a concurrent claim (mark_running, claim_stale_job)
    either committed before — and we see 'running' — or runs after and finds
    no 'pending' row.

    A 'running' row is not stopped (the work is already being done and paid
    for), but the wish is recorded (BR10b, cancel_requested_at, idempotent):
    should this run park itself (defer_job) or lose its worker, it ends as
    'cancelled' instead of being started again. A run that completes ends
    done/error as usual. Ownership is the caller's check (routes.py); the
    store does not know who is asking."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.fetchval(
                "SELECT status FROM ai_jobs WHERE job_id = $1 FOR UPDATE", job_id
            )
            if status is None:
                return None
            if status == JOB_STATUS_RUNNING:
                await conn.execute(
                    """
                    UPDATE ai_jobs
                       SET cancel_requested_at = COALESCE(cancel_requested_at, NOW())
                     WHERE job_id = $1
                    """,
                    job_id,
                )
                return {"status": status, "changed": False, "cancel_requested": True}
            if status != JOB_STATUS_PENDING:
                return {"status": status, "changed": False, "cancel_requested": False}
            await conn.execute(
                """
                UPDATE ai_jobs SET status = 'cancelled', updated_at = NOW(),
                                   finished_at = NOW()
                 WHERE job_id = $1
                """,
                job_id,
            )
    return {"status": JOB_STATUS_CANCELLED, "changed": True, "cancel_requested": False}


async def heartbeat(job_id: str) -> None:
    """Raises JobRowMissing when the row is gone (see there)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE ai_jobs SET heartbeat_at = NOW(), updated_at = NOW() WHERE job_id = $1",
            job_id,
        )
    if _affected(result) == 0:
        raise JobRowMissing("heartbeat", job_id)


async def update_progress(job_id: str, progress: Dict[str, Any]) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE ai_jobs SET progress = $2::jsonb, heartbeat_at = NOW(), updated_at = NOW()
             WHERE job_id = $1
            """,
            job_id,
            json.dumps(progress, default=str),
        )


async def mark_done(job_id: str, result: Optional[Dict[str, Any]]) -> None:
    """Raises JobRowMissing when the row is gone — the result would be lost."""
    pool = get_pool()
    async with pool.acquire() as conn:
        tag = await conn.execute(
            """
            UPDATE ai_jobs SET status = 'done', result = $2::jsonb,
                               updated_at = NOW(), finished_at = NOW()
             WHERE job_id = $1
            """,
            job_id,
            json.dumps(result, default=str) if result is not None else None,
        )
    if _affected(tag) == 0:
        raise JobRowMissing("mark_done", job_id)


async def mark_error(
    job_id: str,
    message: str,
    code: Optional[str] = None,
    retryable: Optional[bool] = None,
    retry_after_s: Optional[int] = None,
) -> None:
    """Terminal error. `retryable`/`retry_after_s` are the verdict of
    src/error_contract.py; when a caller has none (a writer from before BR9),
    the row keeps {message, code} and GET derives the verdict from the code.

    Raises JobRowMissing when the row is gone."""
    error: Dict[str, Any] = {"message": message, "code": code}
    if retryable is not None:
        error["retryable"] = bool(retryable)
        error["retry_after_s"] = retry_after_s
    pool = get_pool()
    async with pool.acquire() as conn:
        tag = await conn.execute(
            """
            UPDATE ai_jobs SET status = 'error', error = $2::jsonb,
                               updated_at = NOW(), finished_at = NOW()
             WHERE job_id = $1
            """,
            job_id,
            json.dumps(error, default=str),
        )
    if _affected(tag) == 0:
        raise JobRowMissing("mark_error", job_id)


async def cleanup_old(ttl_seconds: int) -> int:
    """Delete TERMINAL jobs whose terminal state is older than ttl_seconds.
    Returns rows removed.

    Kept under its old name because workers from before BR11 still call it
    through POST /v1/internal/jobs-maintenance/cleanup. Its old body deleted by
    created_at whatever the status (BR10R M-B); a platform-api with this body
    makes that route safe for old callers too. New callers use prune_jobs."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"""
            DELETE FROM ai_jobs
             WHERE status IN {_TERMINAL_SQL}
               AND {_RETENTION_CLOCK} < NOW() - ($1 || ' seconds')::interval
            """,
            str(ttl_seconds),
        )
    return _affected(result)


async def prune_jobs(terminal_ttl_seconds: int, max_age_seconds: int) -> Dict[str, Any]:
    """One retention pass (BR11). Three parts, each bounded to its own rows:

    1. Delete terminal rows (done/error/cancelled) whose terminal state is
       older than terminal_ttl_seconds. Nothing else is ever deleted.
    2. Backstop for parked work: a 'pending' row older than max_age_seconds
       (counted from submit) is turned into a terminal error
       JOB_MAX_AGE_EXCEEDED — not deleted, so the poller gets a truthful
       answer, and from then on it ages out like any terminal row. The UPDATE
       re-checks status='pending' under the row lock, so a job claimed in the
       same instant stays with its runner.
    3. 'running' rows older than max_age_seconds are only counted. A running
       job belongs to its runner (heartbeat) or to the watchdog (stale) —
       retention does not take it away from either.

    Returns {"removed": n, "expired": [job_id, ...], "running_over_max_age": n}.
    """
    from src.error_contract import JOB_CODE_MAX_AGE_EXCEEDED, job_code_fields

    error = {
        "message": (
            f"Job was still waiting {max_age_seconds // 3600} h after submit "
            f"(capacity or dependency never became available) and was ended by "
            f"the retention backstop."
        ),
        "code": JOB_CODE_MAX_AGE_EXCEEDED,
        **job_code_fields(JOB_CODE_MAX_AGE_EXCEEDED),
    }
    pool = get_pool()
    async with pool.acquire() as conn:
        removed = _affected(await conn.execute(
            f"""
            DELETE FROM ai_jobs
             WHERE status IN {_TERMINAL_SQL}
               AND {_RETENTION_CLOCK} < NOW() - ($1 || ' seconds')::interval
            """,
            str(terminal_ttl_seconds),
        ))
        expired_rows = await conn.fetch(
            """
            UPDATE ai_jobs
               SET status = 'error', error = $2::jsonb,
                   updated_at = NOW(), finished_at = NOW()
             WHERE status = 'pending'
               AND created_at < NOW() - ($1 || ' seconds')::interval
            RETURNING job_id
            """,
            str(max_age_seconds),
            json.dumps(error, default=str),
        )
        running_over = await conn.fetchval(
            """
            SELECT COUNT(*) FROM ai_jobs
             WHERE status = 'running'
               AND created_at < NOW() - ($1 || ' seconds')::interval
            """,
            str(max_age_seconds),
        )
    return {
        "removed": removed,
        "expired": [r["job_id"] for r in expired_rows],
        "running_over_max_age": int(running_over or 0),
    }


# "Stale" is measured from the last sign of life: heartbeat_at while running, or
# created_at for a 'pending' job whose dispatching worker died before it ever
# started. COALESCE collapses both into one notion so neither a never-started nor
# a mid-run-orphaned job is ever stranded.
_STALE_SINCE = "COALESCE(heartbeat_at, created_at)"


# A job waiting on a dependency must not be claimable until its wait expires.
# Pre-existing rows have deferred_until IS NULL and are unaffected.
_NOT_DEFERRED = "(deferred_until IS NULL OR deferred_until <= NOW())"

# A parked job whose wait is OVER is claimable at once — it must not also sit
# out the stale window. defer_job stamps heartbeat_at=NOW(), so under the stale
# rule alone a 30 s capacity wait became 90 s + one watchdog tick (measured
# 29.09.2026: 3-5 s jobs finishing after 90-130 s). Only 'pending' qualifies:
# a claimed row is 'running' again and falls back under the stale rule, so a
# worker that dies mid-run is still detected by its frozen heartbeat.
_DEFER_DUE = "(status = 'pending' AND deferred_until IS NOT NULL AND deferred_until <= NOW())"

# Claimable = (stale, i.e. dead worker / never started) OR (parked and due).
# $1 = stale_seconds.
_CLAIMABLE = (
    f"({_STALE_SINCE} < NOW() - ($1 || ' seconds')::interval OR {_DEFER_DUE})"
)

# The crash-retry budget is evaluated on starts that were NOT dependency waits,
# so waiting out a long outage never consumes it (see migration 044).
_CRASH_ATTEMPTS = "(attempts - defer_count)"


async def defer_job(job_id: str, delay_seconds: int, reason: str) -> None:
    """Park a job until `delay_seconds` from now because a DEPENDENCY was
    unreachable — not because the job or the worker failed.

    Returns it to 'pending' so the existing watchdog picks it up again once
    `deferred_until` passes. Bumps defer_count (which the retry cap subtracts
    out) so an outage of any length cannot exhaust the crash budget, while a
    bounded defer_count still guarantees eventual fail-loud.

    A job whose owner asked to cancel it while it ran (BR10b,
    cancel_requested_at) is NOT parked: it becomes 'cancelled' in the same
    statement, so no watchdog can ever start it again. The caller learns the
    outcome from the row (registry._defer_job reads it for its log line); the
    platform seam stays 204-without-body so a pre-BR10b worker keeps working.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE ai_jobs
               SET status = CASE WHEN cancel_requested_at IS NULL
                                 THEN 'pending' ELSE 'cancelled' END,
                   -- BR11: the 'cancelled' branch is terminal; its retention
                   -- clock starts here.
                   finished_at = CASE WHEN cancel_requested_at IS NULL
                                      THEN finished_at ELSE NOW() END,
                   deferred_until = NOW() + ($2 || ' seconds')::interval,
                   defer_count = defer_count + 1,
                   defer_reason = $3,
                   heartbeat_at = NOW(),
                   updated_at = NOW()
             WHERE job_id = $1
            """,
            job_id,
            str(delay_seconds),
            reason[:500],
        )


async def claim_stale_job(stale_seconds: int, max_attempts: int) -> Optional[Dict[str, Any]]:
    """Atomically claim ONE stale-but-retryable job for requeue and return it
    (now status='running', attempts bumped). Returns None when none are claimable.

    Multi-worker safe: `FOR UPDATE SKIP LOCKED` guarantees that when several
    workers run the watchdog at once, each claims a DIFFERENT row — never the same
    job twice (which would mean paying for the same call twice). Covers both
    'pending' (never started) and 'running' (worker died mid-run); the retry cap
    is enforced here so an unrecoverable job is left for find_abandoned().

    Dependency-deferred jobs are skipped until their wait expires, and their
    waits are subtracted from the retry cap (see migration 044). Once the wait
    HAS expired a parked 'pending' job is claimable immediately (_DEFER_DUE),
    without additionally waiting out `stale_seconds`.

    A row its owner asked to cancel while it ran (BR10b, cancel_requested_at)
    is never claimed: a 'pending' one (parked by a pre-BR10b worker's
    defer_job) is closed as 'cancelled' at once, a 'running' one as soon as its
    heartbeat is stale (dead worker) — in the same call, before the claim, so
    the watchdog neither re-runs it nor leaves it non-terminal. A live run
    with a fresh heartbeat is left alone."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            UPDATE ai_jobs
               SET status = 'cancelled', updated_at = NOW(), finished_at = NOW()
             WHERE cancel_requested_at IS NOT NULL
               AND (status = 'pending'
                    OR (status = 'running'
                        AND {_STALE_SINCE} < NOW() - ($1 || ' seconds')::interval))
            """,
            str(stale_seconds),
        )
        row = await conn.fetchrow(
            f"""
            UPDATE ai_jobs
               SET status = 'running', attempts = attempts + 1,
                   heartbeat_at = NOW(), updated_at = NOW()
             WHERE job_id = (
                 SELECT job_id FROM ai_jobs
                  WHERE status IN ('pending', 'running')
                    AND cancel_requested_at IS NULL
                    AND {_CLAIMABLE}
                    AND {_CRASH_ATTEMPTS} < $2
                    AND {_NOT_DEFERRED}
                  ORDER BY {_STALE_SINCE} ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
             )
            RETURNING *
            """,
            str(stale_seconds),
            max_attempts,
        )
    return _row_to_job(row) if row else None


async def find_abandoned(stale_seconds: int, max_attempts: int) -> List[Dict[str, Any]]:
    """Stale jobs (pending OR running) that have EXHAUSTED their retry budget —
    the watchdog fails these loud ('error') so nothing sits non-terminal forever.

    A job still inside its dependency wait is NOT abandoned — it is waiting on
    purpose. Its own bound is defer_count (enforced by the runner), so excluding
    it here cannot make it immortal. A row with a cancel wish (BR10b) is not
    failed here either: claim_stale_job, which the watchdog runs first, closes
    it as 'cancelled'."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM ai_jobs
             WHERE status IN ('pending', 'running')
               AND cancel_requested_at IS NULL
               AND {_STALE_SINCE} < NOW() - ($1 || ' seconds')::interval
               AND {_CRASH_ATTEMPTS} >= $2
               AND {_NOT_DEFERRED}
             LIMIT 50
            """,
            str(stale_seconds),
            max_attempts,
        )
    return [_row_to_job(r) for r in rows]


async def find_active(origin: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """Jobs that are using, or are about to use, a platform-api right now.
    Used by the deploy gate that runs before platform-api is recreated
    (scripts/platform-api-job-gate.py).

    ``active``: running, or pending and due (no wait, or wait over). These are
    being executed or will be claimed at once. ``waiting``: parked with a wait
    still running. Counted separately, because a parked job does not call
    anyone until its wait ends. ``origin`` limits the result to jobs whose
    budget home (attribution.bridge_origin, ADR-0011) is that bridge. Those are
    the jobs on THIS store's workers that ask the home bridge's platform-api for
    pin, identity and budget.
    """
    if not (1 <= limit <= 200):
        raise ValueError(f"limit must be 1–200, got {limit}")
    params: List[Any] = []
    origin_clause = ""
    if origin is not None:
        params.append(origin.strip().lower())
        origin_clause = f"AND attribution->>'bridge_origin' = ${len(params)}"
    # Counted in full (the gate needs the true number); only the listing is
    # capped at ``limit``. Non-terminal rows are few: the TTL cleanup keeps the
    # table to the last hour or so.
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT job_id, kind, status, attribution->>'bridge_origin' AS origin,
                   updated_at, deferred_until,
                   (status = 'running' OR {_NOT_DEFERRED}) AS is_active
              FROM ai_jobs
             WHERE status IN ('pending', 'running')
               {origin_clause}
             ORDER BY updated_at DESC
            """,
            *params,
        )
    active = [r for r in rows if r["is_active"]]
    waiting = [r for r in rows if not r["is_active"]]

    def _item(r) -> Dict[str, Any]:
        return {
            "job_id": r["job_id"],
            "kind": r["kind"],
            "status": r["status"],
            "origin": r["origin"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "deferred_until": (
                r["deferred_until"].isoformat() if r["deferred_until"] else None
            ),
        }

    return {
        "active": len(active),
        "waiting": len(waiting),
        "jobs": [_item(r) for r in (active + waiting)[:limit]],
    }


# Valid status values for the list-filter guard (fail loud on unknown status).
_VALID_STATUSES = frozenset([
    JOB_STATUS_PENDING, JOB_STATUS_RUNNING, JOB_STATUS_DONE, JOB_STATUS_ERROR,
    JOB_STATUS_CANCELLED,
])


async def list_jobs(
    *,
    app_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return up to `limit` jobs scoped to app_id/user_id, newest first.

    Requires at least one of app_id/user_id — raises ValueError otherwise.
    limit is capped at 200; an out-of-range value raises ValueError.

    NOTE: querying attribution JSONB keys without a functional index means a
    sequential scan on large tables. A future migration should add:
      CREATE INDEX idx_ai_jobs_attr_app  ON ai_jobs ((attribution->>'app_id'));
      CREATE INDEX idx_ai_jobs_attr_user ON ai_jobs ((attribution->>'user_id'));
    """
    if app_id is None and user_id is None:
        raise ValueError("list_jobs requires at least one of app_id or user_id")
    if not (1 <= limit <= 200):
        raise ValueError(f"limit must be 1–200, got {limit}")
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(f"Unknown status filter '{status}'. Valid: {sorted(_VALID_STATUSES)}")

    conditions: List[str] = []
    params: List[Any] = []

    if app_id is not None:
        params.append(app_id)
        conditions.append(f"attribution->>'app_id' = ${len(params)}")

    if user_id is not None:
        params.append(user_id)
        conditions.append(f"attribution->>'user_id' = ${len(params)}")

    if status is not None:
        params.append(status)
        conditions.append(f"status = ${len(params)}")

    params.append(limit)
    limit_placeholder = f"${len(params)}"

    where = " AND ".join(conditions)
    query = f"""
        SELECT job_id, kind, status, attribution, result, progress, created_at, updated_at
          FROM ai_jobs
         WHERE {where}
         ORDER BY created_at DESC
         LIMIT {limit_placeholder}
    """

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return [_row_to_list_item(r) for r in rows]


def _row_to_list_item(row) -> Dict[str, Any]:
    """Slim projection for list responses — no payload (potentially large), no error body."""
    attribution = _loads(row["attribution"])
    result = _loads(row["result"])
    progress = _loads(row["progress"])
    status = row["status"]

    # Extract model/usage best-effort: chat completions put them directly in result
    # (OpenAI-style); other kinds may surface model in progress.
    model: Optional[str] = None
    usage: Optional[Any] = None
    if isinstance(result, dict):
        model = result.get("model")
        usage = result.get("usage")
    if model is None and isinstance(progress, dict):
        model = progress.get("model")

    created = row["created_at"]
    updated = row["updated_at"]
    elapsed: Optional[float] = None
    if isinstance(created, datetime) and isinstance(updated, datetime):
        terminal = status in JOB_TERMINAL_STATUSES
        end = updated if terminal else datetime.now(timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        elapsed = round((end - created).total_seconds(), 2)

    return {
        "job_id": row["job_id"],
        "kind": row["kind"],
        "status": status,
        "created_at": created.isoformat() if isinstance(created, datetime) else created,
        "elapsed_seconds": elapsed,
        "model": model,
        "usage": usage,
        "attribution": attribution,
    }
