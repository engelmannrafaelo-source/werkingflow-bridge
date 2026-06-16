"""Executor registry + generic background runner for async jobs.

An *executor* turns a job's `payload` into a result dict for a given `kind`:

    async def executor(payload: dict, attribution: dict | None, report_progress) -> dict

`report_progress(progress: dict)` is an awaitable the executor MAY call to push
incremental progress/partial output into the store (drives polling/streaming UX).

main.py registers executors at startup (so this module stays free of app/main.py
imports and import cycles). The runner mirrors _run_async_research_job but persists
to Postgres and keeps a heartbeat alive for the whole run, so the watchdog can tell
a genuinely-running job (fresh heartbeat) from a worker that died mid-job (stale).
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.jobs import store

logger = logging.getLogger(__name__)

# Executor contract: (payload, attribution, report_progress) -> result dict.
Executor = Callable[[Dict[str, Any], Optional[Dict[str, Any]], Callable[[Dict[str, Any]], Awaitable[None]]], Awaitable[Dict[str, Any]]]

_EXECUTORS: Dict[str, Executor] = {}

# Heartbeat cadence while a job runs. Must be well below the watchdog stale
# window so a healthy long job never looks dead.
HEARTBEAT_INTERVAL_S = 15


def register_executor(kind: str, executor: Executor) -> None:
    """Idempotent-ish: last registration wins (startup runs once per worker)."""
    _EXECUTORS[kind] = executor
    logger.info(f"🧩 Registered async-job executor: kind={kind!r}")


def get_executor(kind: str) -> Optional[Executor]:
    return _EXECUTORS.get(kind)


def registered_kinds() -> List[str]:
    return sorted(_EXECUTORS.keys())


async def run_generic_job(
    job_id: str,
    kind: str,
    payload: Dict[str, Any],
    attribution: Optional[Dict[str, Any]],
) -> None:
    """Background runner: mark running → execute → mark done/error. Keeps a
    heartbeat ticking for the whole run so the watchdog only requeues dead workers.
    Never raises — a crash is recorded as status='error' (fail loud, queryable)."""
    await store.mark_running(job_id)

    executor = get_executor(kind)
    if executor is None:
        await store.mark_error(job_id, f"No executor registered for kind '{kind}'", code="NO_EXECUTOR")
        logger.error(f"❌ Async job {job_id}: no executor for kind={kind!r}")
        return

    stop = asyncio.Event()

    async def _heartbeat_loop() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_S)
            except asyncio.TimeoutError:
                try:
                    await store.heartbeat(job_id)
                except Exception as e:  # heartbeat failure must not kill the job
                    logger.warning(f"⚠️ Heartbeat failed for job {job_id}: {e}")

    hb_task = asyncio.create_task(_heartbeat_loop())

    async def report_progress(progress: Dict[str, Any]) -> None:
        await store.update_progress(job_id, progress)

    try:
        result = await executor(payload, attribution, report_progress)
        await store.mark_done(job_id, result)
        logger.info(f"📦 Async job {job_id} (kind={kind}) finished: done")
    except Exception as e:
        await store.mark_error(job_id, str(e), code="EXECUTOR_ERROR")
        logger.error(f"❌ Async job {job_id} (kind={kind}) crashed: {e}", exc_info=True)
    finally:
        stop.set()
        try:
            await hb_task
        except Exception:
            pass


async def run_watchdog_pass(stale_seconds: int, max_attempts: int) -> Dict[str, int]:
    """One watchdog sweep: requeue stale-but-retryable 'running' jobs, fail-loud
    the ones that exhausted retries. Returns counts. Called periodically by main.py."""
    requeued = 0
    failed = 0

    for job in await store.find_stale_running(stale_seconds, max_attempts):
        # Re-dispatch — mark_running bumps attempts, so the cap is honored.
        asyncio.create_task(
            run_generic_job(job["job_id"], job["kind"], job.get("payload") or {}, job.get("attribution"))
        )
        requeued += 1
        logger.warning(f"♻️ Watchdog requeued stale job {job['job_id']} (attempt {job['attempts'] + 1})")

    for job in await store.find_dead_running(stale_seconds, max_attempts):
        await store.mark_error(
            job["job_id"],
            f"Job lost after {job['attempts']} attempts (worker death, retries exhausted)",
            code="REQUEUE_EXHAUSTED",
        )
        failed += 1
        logger.error(f"💀 Watchdog failed-loud job {job['job_id']} (retries exhausted)")

    return {"requeued": requeued, "failed": failed}
