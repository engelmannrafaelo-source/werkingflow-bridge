"""Worker-side access to the durable job store (ADR-0009 Weg b, Schritt 2d).

Same function names and signatures as src.jobs.store — call sites
(src.jobs.routes, src.jobs.registry, main.py's maintenance loop) switch their
import and nothing else. The SQL itself stays in store.py, which after this
step is platform-api's module (served via /v1/internal/jobs*); this module is
the HTTP counterpart, in the established three-stage shape (principals.py,
prepaid_cap.py):

  1. platform-api via platform_client.call_platform (service-token auth),
  2. on PlatformUnavailable: direct-DB fallback through store.* — exists only
     while workers still carry BRIDGE_DB_URL, and is the instant rollback
     during live validation ("Alter Weg bleibt als sofortiger Rueckfall"),
  3. neither configured: JobStoreUnavailable, named and loud.

No retries anywhere (call_platform's opt-in default stays 0). Two operations
bump counters and are therefore not idempotent — mark_running/defer_job
(attempts/defer_count) and claim_stale_job (an atomic claim). For those the
fallback after a TIMEOUT can double-apply: the platform call may have executed
and only the answer got lost. Consequences are bounded and money-free — a
double-bumped attempts counter spends retry budget early, a doubly-claimed job
is re-run by the watchdog after the stale window (same self-healing that
already covers a worker dying right after a claim). Documented here instead of
"fixed" with a dedup mechanism the job table deliberately doesn't need.
Since BR10 mark_running is a conditional claim (pending only): a double-apply
there means the platform call claimed the row, the fallback finds it no longer
'pending' and reports "not claimed", so the worker does not run it — the row
sits at 'running' with a frozen heartbeat until the watchdog re-claims it after
the stale window. Same bounded self-healing, no double run.

Datetimes: store.* returns datetime objects (asyncpg); over HTTP they arrive
as ISO strings. _revive_job parses them back so callers (elapsed computation
in routes.py, defer_count logic in registry.py) see the exact same types on
both stages.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.db.client import is_db_enabled
from src.jobs import store
from src.platform_client import PlatformUnavailable, call_platform

logger = logging.getLogger(__name__)

# Job payloads/results can be full chat completions — give writes carrying a
# body more room than the 2s read default before declaring platform-api gone.
_WRITE_TIMEOUT_S = 10.0
_READ_TIMEOUT_S = 5.0

_DATETIME_FIELDS = ("created_at", "updated_at", "heartbeat_at", "deferred_until")


class JobStoreUnavailable(RuntimeError):
    """Neither platform-api answered nor a direct DB connection exists — the
    job store is genuinely unreachable from this process. Named so the failure
    reads as what it is, not as a get_pool() RuntimeError cascade."""


def is_store_available() -> bool:
    """Can this process reach a job store at all (either stage)?

    Replaces the bare is_db_enabled() gate in routes.py/main.py: a worker
    without BRIDGE_DB_URL but with a configured platform path is fully
    job-capable. BRIDGE_SERVICE_TOKEN is the signal for the platform stage —
    PLATFORM_API_URL always has a default, the token does not (same reasoning
    as user_provider_override's wiring check).
    """
    import os

    return is_db_enabled() or bool(os.getenv("BRIDGE_SERVICE_TOKEN"))


def _revive_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Parse ISO-string datetime fields back to datetime objects in place."""
    if job is None:
        return None
    for field in _DATETIME_FIELDS:
        val = job.get(field)
        if isinstance(val, str):
            try:
                job[field] = datetime.fromisoformat(val)
            except ValueError:
                # A malformed timestamp must not eat the whole job row — but it
                # is a contract violation worth seeing, not a silent None.
                logger.error("job %s: unparseable %s=%r from platform-api",
                             job.get("job_id"), field, val)
    return job


def _fallback_or_raise(op: str, e: PlatformUnavailable) -> None:
    """Shared stage-2/3 decision: log loud, then either return (caller runs the
    direct-DB fallback) or raise JobStoreUnavailable when there is none."""
    if is_db_enabled():
        logger.error("job store %s via platform-api failed (%s) — falling back to direct DB", op, e)
        return
    raise JobStoreUnavailable(
        f"job store {op}: platform-api unavailable ({e}) and this process has "
        f"no direct-DB fallback (BRIDGE_DB_URL unset)"
    ) from e


def _unexpected(op: str, status: int, body: Any) -> JobStoreUnavailable:
    """A status the contract doesn't know is a broken contract, not a datum —
    fail loud instead of guessing (no silent fallback: the platform DID answer)."""
    return JobStoreUnavailable(
        f"job store {op}: platform-api answered unexpectedly "
        f"(status={status}, body={str(body)[:200]})"
    )


async def create_job(
    job_id: str,
    kind: str,
    payload: Optional[Dict[str, Any]],
    attribution: Optional[Dict[str, Any]],
) -> None:
    try:
        resp = await call_platform(
            "POST", "/v1/internal/jobs",
            json={"job_id": job_id, "kind": kind, "payload": payload, "attribution": attribution},
            timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("create_job", e)
        return await store.create_job(job_id, kind, payload, attribution)
    if resp.status_code != 204:
        raise _unexpected("create_job", resp.status_code, resp.json)


async def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        resp = await call_platform(
            "GET", f"/v1/internal/jobs/{job_id}", timeout_s=_READ_TIMEOUT_S
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("get_job", e)
        return await store.get_job(job_id)
    if resp.status_code == 404:
        return None
    if resp.status_code == 200 and isinstance(resp.json, dict) and "job" in resp.json:
        return _revive_job(resp.json["job"])
    raise _unexpected("get_job", resp.status_code, resp.json)


async def mark_running(job_id: str) -> bool:
    """True = this worker claimed the fresh job; False = it was not 'pending'
    any more (cancelled by its owner, or already claimed) and must not run.

    204 = claimed, 409 = not claimed. A platform-api from before BR10 answers
    204 unconditionally — correct there, because that platform-api has no
    cancel route either, so a row cannot have been cancelled. A pre-BR10
    worker against a BR10 platform-api reads the 409 as an unexpected status
    and raises before running the job, which is the right outcome too."""
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/mark-running", timeout_s=_WRITE_TIMEOUT_S
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("mark_running", e)
        return await store.mark_running(job_id)
    if resp.status_code == 204:
        return True
    if resp.status_code == 409:
        return False
    raise _unexpected("mark_running", resp.status_code, resp.json)


async def cancel_job(job_id: str) -> Optional[Dict[str, Any]]:
    """store.cancel_job over the platform seam (BR10). None = unknown id.

    The route answers 200 for every known outcome, including "not found"
    (``{"job": null}``), so that a 404/405 can only mean "this platform-api has
    no cancel route" (deployed before BR10) — loud, never read as "no such
    job"."""
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/cancel", timeout_s=_WRITE_TIMEOUT_S
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("cancel_job", e)
        return await store.cancel_job(job_id)
    if resp.status_code == 200 and isinstance(resp.json, dict) and "job" in resp.json:
        job = resp.json["job"]
        if job is None:
            return None
        if isinstance(job, dict) and "status" in job and "changed" in job:
            return job
    raise _unexpected("cancel_job", resp.status_code, resp.json)


async def heartbeat(job_id: str) -> None:
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/heartbeat", timeout_s=_READ_TIMEOUT_S
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("heartbeat", e)
        return await store.heartbeat(job_id)
    if resp.status_code != 204:
        raise _unexpected("heartbeat", resp.status_code, resp.json)


async def update_progress(job_id: str, progress: Dict[str, Any]) -> None:
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/progress",
            json={"progress": progress}, timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("update_progress", e)
        return await store.update_progress(job_id, progress)
    if resp.status_code != 204:
        raise _unexpected("update_progress", resp.status_code, resp.json)


async def mark_done(job_id: str, result: Optional[Dict[str, Any]]) -> None:
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/done",
            json={"result": result}, timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("mark_done", e)
        return await store.mark_done(job_id, result)
    if resp.status_code != 204:
        raise _unexpected("mark_done", resp.status_code, resp.json)


async def mark_error(
    job_id: str,
    message: str,
    code: Optional[str] = None,
    retryable: Optional[bool] = None,
    retry_after_s: Optional[int] = None,
) -> None:
    # A platform-api from before BR9 ignores retryable/retry_after_s (pydantic
    # drops unknown fields) and stores {message, code}; GET then derives the
    # verdict from the code (error_contract.job_code_fields). Degraded, not wrong
    # for any code except a research/transport failure, which reads as final.
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/error",
            json={"message": message, "code": code,
                  "retryable": retryable, "retry_after_s": retry_after_s},
            timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("mark_error", e)
        return await store.mark_error(
            job_id, message, code=code,
            retryable=retryable, retry_after_s=retry_after_s,
        )
    if resp.status_code != 204:
        raise _unexpected("mark_error", resp.status_code, resp.json)


async def defer_job(job_id: str, delay_seconds: int, reason: str) -> None:
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/defer",
            json={"delay_seconds": delay_seconds, "reason": reason},
            timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("defer_job", e)
        return await store.defer_job(job_id, delay_seconds, reason)
    if resp.status_code != 204:
        raise _unexpected("defer_job", resp.status_code, resp.json)


async def claim_stale_job(stale_seconds: int, max_attempts: int) -> Optional[Dict[str, Any]]:
    try:
        resp = await call_platform(
            "POST", "/v1/internal/jobs-maintenance/claim-stale",
            json={"stale_seconds": stale_seconds, "max_attempts": max_attempts},
            timeout_s=_READ_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("claim_stale_job", e)
        return await store.claim_stale_job(stale_seconds, max_attempts)
    if resp.status_code == 200 and isinstance(resp.json, dict) and "job" in resp.json:
        return _revive_job(resp.json["job"])
    raise _unexpected("claim_stale_job", resp.status_code, resp.json)


async def find_abandoned(stale_seconds: int, max_attempts: int) -> List[Dict[str, Any]]:
    try:
        resp = await call_platform(
            "GET", "/v1/internal/jobs-maintenance/abandoned",
            params={"stale_seconds": stale_seconds, "max_attempts": max_attempts},
            timeout_s=_READ_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("find_abandoned", e)
        return await store.find_abandoned(stale_seconds, max_attempts)
    if resp.status_code == 200 and isinstance(resp.json, dict) and isinstance(resp.json.get("jobs"), list):
        return [_revive_job(j) for j in resp.json["jobs"]]
    raise _unexpected("find_abandoned", resp.status_code, resp.json)


async def cleanup_old(ttl_seconds: int) -> int:
    try:
        resp = await call_platform(
            "POST", "/v1/internal/jobs-maintenance/cleanup",
            json={"ttl_seconds": ttl_seconds}, timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("cleanup_old", e)
        return await store.cleanup_old(ttl_seconds)
    if resp.status_code == 200 and isinstance(resp.json, dict) and "removed" in resp.json:
        return int(resp.json["removed"])
    raise _unexpected("cleanup_old", resp.status_code, resp.json)


async def list_jobs(
    *,
    app_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"limit": limit}
    if app_id is not None:
        params["app_id"] = app_id
    if user_id is not None:
        params["user_id"] = user_id
    if status is not None:
        params["status"] = status
    try:
        resp = await call_platform(
            "GET", "/v1/internal/jobs", params=params, timeout_s=_READ_TIMEOUT_S
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("list_jobs", e)
        return await store.list_jobs(app_id=app_id, user_id=user_id, status=status, limit=limit)
    if resp.status_code == 400:
        # The internal route maps the store's ValueError to 400 — re-raise as
        # the same type so both stages present one contract to routes.py.
        detail = (resp.json or {}).get("detail") if isinstance(resp.json, dict) else None
        raise ValueError(detail or "invalid list_jobs arguments")
    if resp.status_code == 200 and isinstance(resp.json, dict) and isinstance(resp.json.get("jobs"), list):
        return resp.json["jobs"]
    raise _unexpected("list_jobs", resp.status_code, resp.json)
