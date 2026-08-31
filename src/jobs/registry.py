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
  is recovered by the watchdog: store_client.claim_stale_job atomically claims it
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

from src.jobs import store_client

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

# ---------------------------------------------------------------------------
# Dependency deferral
# ---------------------------------------------------------------------------
# 424 Failed Dependency is the bridge's answer when a required downstream (today:
# the privacy service) could not be REACHED. Deliberately not 5xx: nginx's
# proxy_next_upstream retries 500/502/503/504/429 across every worker, and every
# worker shares the same downstream URL — so a 5xx here would burn all workers on
# a hop that cannot succeed and then surface as the misleading "bridge at
# capacity" envelope (exactly the 2026-08-01 misdiagnosis).
DEPENDENCY_UNAVAILABLE_STATUS = 424

# Backoff between dependency retries, and the total number of waits allowed.
# 60s × 240 ≈ 4h of patience: long enough to sit out a real host outage
# (2026-08-01 lasted ~23 min), bounded so a permanently dead dependency still
# fails loud instead of parking work forever.
DEPENDENCY_RETRY_DELAY_S = 60
DEPENDENCY_MAX_DEFERS = 240


async def _defer_for_dependency(job_id: str, kind: str, reason: str) -> bool:
    """Park a job waiting on an unreachable dependency.

    Returns True when the job was deferred (caller must stop), False when the
    patience budget is spent and it should fail loud like any other error.
    """
    job = await store_client.get_job(job_id)
    deferred_so_far = (job or {}).get("defer_count") or 0
    if deferred_so_far >= DEPENDENCY_MAX_DEFERS:
        logger.error(
            f"💀 Async job {job_id} (kind={kind}) gave up after {deferred_so_far} "
            f"dependency waits (~{deferred_so_far * DEPENDENCY_RETRY_DELAY_S // 60}min): {reason}"
        )
        return False
    await store_client.defer_job(job_id, DEPENDENCY_RETRY_DELAY_S, reason)
    logger.warning(
        f"⏸️ Async job {job_id} (kind={kind}) deferred {DEPENDENCY_RETRY_DELAY_S}s "
        f"(wait {deferred_so_far + 1}/{DEPENDENCY_MAX_DEFERS}) — dependency unreachable: {reason}"
    )
    return True


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
    await store_client.mark_running(job_id)
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
        await store_client.mark_error(job_id, f"No executor registered for kind '{kind}'", code="NO_EXECUTOR")
        logger.error(f"❌ Async job {job_id}: no executor for kind={kind!r}")
        return

    stop = asyncio.Event()

    async def _heartbeat_loop() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_S)
            except asyncio.TimeoutError:
                try:
                    await store_client.heartbeat(job_id)
                except Exception as e:  # heartbeat failure must not kill the job
                    logger.warning(f"⚠️ Heartbeat failed for job {job_id}: {e}")

    hb_task = asyncio.create_task(_heartbeat_loop())

    async def report_progress(progress: Dict[str, Any]) -> None:
        await store_client.update_progress(job_id, progress)

    try:
        result = await executor(payload, attribution, report_progress)
        await store_client.mark_done(job_id, result)
        logger.info(f"📦 Async job {job_id} (kind={kind}) finished: done")
    except Exception as e:
        # Preserve the upstream HTTP status in the error code so clients can
        # restore retry semantics (a 400 must not read as a retryable 502).
        from src.jobs.executors import ExecutorHTTPError

        # A DEPENDENCY that could not be reached is not a failed job — it is a
        # job whose turn has not come. Park it and let the watchdog re-run it
        # once the dependency is back, instead of burning it terminally (which
        # is what made "defer the check until the GPU returns" impossible).
        if isinstance(e, ExecutorHTTPError) and e.status_code == DEPENDENCY_UNAVAILABLE_STATUS:
            deferred = await _defer_for_dependency(job_id, kind, str(e))
            if deferred:
                return

        code = (
            f"UPSTREAM_HTTP_{e.status_code}"
            if isinstance(e, ExecutorHTTPError) else "EXECUTOR_ERROR"
        )
        await store_client.mark_error(job_id, str(e), code=code)
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
        job = await store_client.claim_stale_job(stale_seconds, max_attempts)
        if job is None:
            break
        # Already claimed (status=running, attempts bumped) → run the body only.
        spawn(_run_body(job["job_id"], job["kind"], job.get("payload") or {}, job.get("attribution")))
        requeued += 1
        logger.warning(f"♻️ Watchdog requeued stale job {job['job_id']} (attempt {job['attempts']})")

    failed = 0
    for job in await store_client.find_abandoned(stale_seconds, max_attempts):
        await store_client.mark_error(
            job["job_id"],
            f"Job lost after {job['attempts']} attempts (worker death, retries exhausted)",
            code="REQUEUE_EXHAUSTED",
        )
        failed += 1
        logger.error(f"💀 Watchdog failed-loud job {job['job_id']} (retries exhausted)")

    return {"requeued": requeued, "failed": failed}
