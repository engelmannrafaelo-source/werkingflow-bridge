"""Executor registry + generic background runner for async jobs.

An *executor* turns a job's `payload` into a result dict for a given `kind`:

    async def executor(payload: dict, attribution: dict | None, report_progress) -> dict

`report_progress(progress: dict)` is an awaitable the executor MAY call to push
incremental progress/partial output into the store (drives polling/streaming UX).

main.py registers executors at startup (so this module stays free of app/main.py
imports and import cycles).

Durability model
----------------
- A FRESH job (route) is dispatched via run_generic_job → mark_running → _run_body.
- A STALE job (dispatching worker died at 'pending', or worker died mid-'running')
  is recovered by the watchdog: store.claim_stale_job atomically claims it
  (FOR UPDATE SKIP LOCKED → multi-worker safe, each worker claims a different row)
  and we run _run_body directly — NO second mark_running, so attempts is bumped
  exactly once per (re)start.
- _run_body keeps a heartbeat alive for the whole run, so the watchdog can tell a
  genuinely-running job (fresh heartbeat) from a dead worker (stale).

Background tasks are kept in a module-level set so the event loop's weak reference
cannot let them be garbage-collected mid-flight (CPython asyncio gotcha).
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.jobs import store

logger = logging.getLogger(__name__)

# Executor contract: (payload, attribution, report_progress) -> result dict.
Executor = Callable[[Dict[str, Any], Optional[Dict[str, Any]], Callable[[Dict[str, Any]], Awaitable[None]]], Awaitable[Dict[str, Any]]]

_EXECUTORS: Dict[str, Executor] = {}

# Strong refs to in-flight background tasks — without these the loop keeps only a
# weak ref and a task may be GC'd before it finishes.
_BACKGROUND_TASKS: "set[asyncio.Task]" = set()

# Heartbeat cadence while a job runs. Must be well below the watchdog stale
# window so a healthy long job never looks dead.
HEARTBEAT_INTERVAL_S = 15

# Safety cap on how many stale jobs one watchdog pass requeues (back-pressure).
WATCHDOG_MAX_REQUEUE_PER_PASS = 50


def register_executor(kind: str, executor: Executor) -> None:
    """Idempotent-ish: last registration wins (startup runs once per worker)."""
    _EXECUTORS[kind] = executor
    logger.info(f"🧩 Registered async-job executor: kind={kind!r}")


def get_executor(kind: str) -> Optional[Executor]:
    return _EXECUTORS.get(kind)


def registered_kinds() -> List[str]:
    return sorted(_EXECUTORS.keys())


def spawn(coro: Awaitable[None]) -> "asyncio.Task":
    """Fire a background task and keep a strong reference until it completes."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def run_generic_job(
    job_id: str,
    kind: str,
    payload: Dict[str, Any],
    attribution: Optional[Dict[str, Any]],
) -> None:
    """Entry point for a FRESH job (status 'pending'): claim it for this worker
    (pending → running, attempts 0 → 1), then run the body."""
    await store.mark_running(job_id)
    await _run_body(job_id, kind, payload, attribution)


async def _run_body(
    job_id: str,
    kind: str,
    payload: Dict[str, Any],
    attribution: Optional[Dict[str, Any]],
) -> None:
    """Execute one job that is ALREADY marked 'running'. Heartbeats for the whole
    run; records done/error. Never raises — a crash is persisted as status='error'
    (fail loud, queryable) so the job never sits non-terminal."""
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
    """One watchdog sweep:
      - atomically claim & requeue stale-but-retryable jobs (pending or running),
      - fail-loud the ones that exhausted their retry budget.
    Returns counts. Safe to run concurrently on every worker (atomic claim)."""
    requeued = 0
    while requeued < WATCHDOG_MAX_REQUEUE_PER_PASS:
        job = await store.claim_stale_job(stale_seconds, max_attempts)
        if job is None:
            break
        # Already claimed (status=running, attempts bumped) → run the body only.
        spawn(_run_body(job["job_id"], job["kind"], job.get("payload") or {}, job.get("attribution")))
        requeued += 1
        logger.warning(f"♻️ Watchdog requeued stale job {job['job_id']} (attempt {job['attempts']})")

    failed = 0
    for job in await store.find_abandoned(stale_seconds, max_attempts):
        await store.mark_error(
            job["job_id"],
            f"Job lost after {job['attempts']} attempts (worker death, retries exhausted)",
            code="REQUEUE_EXHAUSTED",
        )
        failed += 1
        logger.error(f"💀 Watchdog failed-loud job {job['job_id']} (retries exhausted)")

    return {"requeued": requeued, "failed": failed}
