"""Postgres-backed durable store for generic async jobs (table: ai_jobs).

No dependency on main.py / app — pure data access, so it is unit-testable and
import-safe. All functions assume the asyncpg pool is initialized
(src.db.client.init_pool, called in main.py's lifespan when BRIDGE_DB_URL is set);
callers gate on is_db_enabled() before reaching here.

JSONB columns are written with an explicit ::jsonb cast on a json.dumps string
and read back with _loads (asyncpg returns jsonb as text by default).
"""
import hashlib
import json
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
    }


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


async def find_stale_running(stale_seconds: int, max_attempts: int) -> List[Dict[str, Any]]:
    """Jobs stuck in 'running' past the heartbeat window and still under the
    retry cap — the watchdog requeues these (worker died mid-job). A job at/over
    max_attempts is left for the watchdog to fail loud instead of looping."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM ai_jobs
             WHERE status = 'running'
               AND heartbeat_at < NOW() - ($1 || ' seconds')::interval
               AND attempts < $2
             ORDER BY heartbeat_at ASC
             LIMIT 50
            """,
            str(stale_seconds),
            max_attempts,
        )
    return [_row_to_job(r) for r in rows]


async def find_dead_running(stale_seconds: int, max_attempts: int) -> List[Dict[str, Any]]:
    """'running' jobs past the heartbeat window that have EXHAUSTED retries —
    these get failed loud ('error') so they never sit in 'running' forever."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM ai_jobs
             WHERE status = 'running'
               AND heartbeat_at < NOW() - ($1 || ' seconds')::interval
               AND attempts >= $2
             LIMIT 50
            """,
            str(stale_seconds),
            max_attempts,
        )
    return [_row_to_job(r) for r in rows]
