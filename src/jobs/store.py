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

# Terminal vs in-flight. Polling clients stop on done/error.
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_DONE = "done"
JOB_STATUS_ERROR = "error"


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


async def mark_running(job_id: str) -> None:
    """Transition to running, bump attempts, stamp heartbeat. Called by the runner."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE ai_jobs
               SET status = 'running', attempts = attempts + 1,
                   heartbeat_at = NOW(), updated_at = NOW()
             WHERE job_id = $1
            """,
            job_id,
        )


async def heartbeat(job_id: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ai_jobs SET heartbeat_at = NOW(), updated_at = NOW() WHERE job_id = $1",
            job_id,
        )


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
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE ai_jobs SET status = 'done', result = $2::jsonb, updated_at = NOW()
             WHERE job_id = $1
            """,
            job_id,
            json.dumps(result, default=str) if result is not None else None,
        )


async def mark_error(job_id: str, message: str, code: Optional[str] = None) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE ai_jobs SET status = 'error', error = $2::jsonb, updated_at = NOW()
             WHERE job_id = $1
            """,
            job_id,
            json.dumps({"message": message, "code": code}, default=str),
        )


async def cleanup_old(ttl_seconds: int) -> int:
    """Delete jobs older than ttl_seconds. Returns rows removed."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM ai_jobs WHERE created_at < NOW() - ($1 || ' seconds')::interval",
            str(ttl_seconds),
        )
    # asyncpg returns e.g. "DELETE 7"
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


# "Stale" is measured from the last sign of life: heartbeat_at while running, or
# created_at for a 'pending' job whose dispatching worker died before it ever
# started. COALESCE collapses both into one notion so neither a never-started nor
# a mid-run-orphaned job is ever stranded.
_STALE_SINCE = "COALESCE(heartbeat_at, created_at)"


# A job waiting on a dependency must not be claimable until its wait expires.
# Pre-existing rows have deferred_until IS NULL and are unaffected.
_NOT_DEFERRED = "(deferred_until IS NULL OR deferred_until <= NOW())"

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
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE ai_jobs
               SET status = 'pending',
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
    waits are subtracted from the retry cap (see migration 044)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE ai_jobs
               SET status = 'running', attempts = attempts + 1,
                   heartbeat_at = NOW(), updated_at = NOW()
             WHERE job_id = (
                 SELECT job_id FROM ai_jobs
                  WHERE status IN ('pending', 'running')
                    AND {_STALE_SINCE} < NOW() - ($1 || ' seconds')::interval
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
    it here cannot make it immortal."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM ai_jobs
             WHERE status IN ('pending', 'running')
               AND {_STALE_SINCE} < NOW() - ($1 || ' seconds')::interval
               AND {_CRASH_ATTEMPTS} >= $2
               AND {_NOT_DEFERRED}
             LIMIT 50
            """,
            str(stale_seconds),
            max_attempts,
        )
    return [_row_to_job(r) for r in rows]


# Valid status values for the list-filter guard (fail loud on unknown status).
_VALID_STATUSES = frozenset([JOB_STATUS_PENDING, JOB_STATUS_RUNNING, JOB_STATUS_DONE, JOB_STATUS_ERROR])


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
        terminal = status in (JOB_STATUS_DONE, JOB_STATUS_ERROR)
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
