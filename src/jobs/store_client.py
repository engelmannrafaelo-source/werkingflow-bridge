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
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.db.client import is_db_enabled
from src.error_contract import JOB_ERROR_CAUSE_ECHO_KEY
from src.jobs import store
from src.platform_client import PlatformUnavailable, call_platform

logger = logging.getLogger(__name__)

# Job payloads/results can be full chat completions — give writes carrying a
# body more room than the 2s read default before declaring platform-api gone.
_WRITE_TIMEOUT_S = 10.0
_READ_TIMEOUT_S = 5.0

_DATETIME_FIELDS = (
    "created_at", "updated_at", "heartbeat_at", "deferred_until", "finished_at",
)

# Re-exported so callers (registry.py) need only this module.
JobRowMissing = store.JobRowMissing


# ── Retention / lost-row counters (BR11) ─────────────────────────────────────
# In-memory per worker since start, same shape as src/attribution.py; read via
# GET /v1/metrics/jobs-retention. Every increment also has its own log line —
# the counter is for "how often", the log for "which job".
_STARTED_AT = time.time()
_lock = threading.Lock()
_row_missing: Dict[str, int] = defaultdict(int)
_retention: Dict[str, int] = defaultdict(int)
# Cause fields a platform-api did not confirm (BRTRUNC2N), by field name.
_cause_dropped: Dict[str, int] = defaultdict(int)


def _count(bucket: Dict[str, int], key: str, n: int = 1) -> None:
    with _lock:
        bucket[key] += n


def retention_snapshot() -> Dict[str, Any]:
    with _lock:
        return {
            "since_epoch": _STARTED_AT,
            "row_missing_by_op": dict(_row_missing),
            "retention": dict(_retention),
            "cause_fields_dropped": dict(_cause_dropped),
        }


def record_retention(key: str, n: int = 1) -> None:
    """Counter hook for the maintenance loop (main.py)."""
    _count(_retention, key, n)


class RetentionRouteMissing(RuntimeError):
    """platform-api does not know POST /v1/internal/jobs-maintenance/prune —
    it predates BR11. Its only retention route is the old /cleanup, which
    deletes by created_at whatever the status; this worker refuses to call it.
    Consequence: nothing is deleted until platform-api is updated (rows grow,
    nothing is lost). Loud, never a silent no-op."""


def _row_missing_error(
    op: str, job_id: str, cause: "BaseException | None" = None
) -> JobRowMissing:
    _count(_row_missing, op)
    logger.error(
        "💀 job store %s: row of job %s is GONE — %s",
        op, job_id,
        "the result of finished work is lost" if op in ("mark_done", "mark_error")
        else "the job can no longer report progress or finish",
    )
    err = JobRowMissing(op, job_id)
    if cause is not None:
        err.__cause__ = cause
    return err


def _is_row_missing(resp: Any) -> bool:
    """404 with reason job_row_missing — the BR11 contract of the write routes.
    A bare 404 (no such reason) is something else and stays _unexpected."""
    body = resp.json if isinstance(resp.json, dict) else {}
    detail = body.get("detail")
    return (
        resp.status_code == 404
        and isinstance(detail, dict)
        and detail.get("reason") == "job_row_missing"
    )


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


# Re-exported for routes.py.
IdempotencyKeyReused = store.IdempotencyKeyReused


class IdempotencyUnsupported(JobStoreUnavailable):
    """platform-api predates BRREF2 and has no create-idempotent route. The
    key cannot be honoured, so NO job is created — a plain create would be
    exactly the silent double job the key exists to prevent."""


async def find_job_by_idempotency_key(
    owner: str, key: str, kind: str, payload: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """store.find_job_by_idempotency_key over the platform seam (IDEMR). A read:
    the direct-DB fallback after a timeout is safe."""
    try:
        resp = await call_platform(
            "POST", "/v1/internal/jobs/idempotency-lookup",
            json={"kind": kind, "payload": payload,
                  "idempotency_owner": owner, "idempotency_key": key},
            timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("find_job_by_idempotency_key", e)
        return await store.find_job_by_idempotency_key(owner, key, kind, payload)
    body = resp.json if isinstance(resp.json, dict) else {}
    if resp.status_code == 200 and "job" in body:
        job = body["job"]
        if job is None or isinstance(job, dict):
            return job
    detail = body.get("detail")
    if (
        resp.status_code == 409
        and isinstance(detail, dict)
        and detail.get("reason") == "idempotency_key_reused"
    ):
        raise IdempotencyKeyReused(str(detail.get("job_id")))
    if resp.status_code in (404, 405):
        raise IdempotencyUnsupported(
            f"job store find_job_by_idempotency_key: platform-api has no "
            f"/v1/internal/jobs/idempotency-lookup (status={resp.status_code}) — "
            f"it predates IDEMR; no job created"
        )
    raise _unexpected("find_job_by_idempotency_key", resp.status_code, resp.json)


async def create_job_idempotent(
    job_id: str,
    kind: str,
    payload: Optional[Dict[str, Any]],
    attribution: Optional[Dict[str, Any]],
    owner: str,
    key: str,
) -> Dict[str, Any]:
    """store.create_job_idempotent over the platform seam (BRREF2).

    Safe to fall back after a timeout, unlike mark_running: if the platform
    call did insert the row and only the answer got lost, the direct-DB call
    finds that row under the same (owner, key) and reports a replay of the
    very job this worker just named. created=False with the caller's OWN
    job_id therefore means "created by me", and routes.py dispatches it
    (mark_running is a conditional claim, so even a second dispatch could not
    run it twice). One job, never two."""
    try:
        resp = await call_platform(
            "POST", "/v1/internal/jobs/create-idempotent",
            json={
                "job_id": job_id, "kind": kind, "payload": payload,
                "attribution": attribution,
                "idempotency_owner": owner, "idempotency_key": key,
            },
            timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("create_job_idempotent", e)
        return await store.create_job_idempotent(
            job_id, kind, payload, attribution, owner, key
        )
    body = resp.json if isinstance(resp.json, dict) else {}
    created_shape = isinstance(body.get("job"), dict) and "created" in body
    if resp.status_code == 200 and created_shape:
        return {"created": bool(body["created"]), "job": body["job"]}
    detail = body.get("detail")
    if (
        resp.status_code == 409
        and isinstance(detail, dict)
        and detail.get("reason") == "idempotency_key_reused"
    ):
        raise IdempotencyKeyReused(str(detail.get("job_id")))
    if resp.status_code in (404, 405):
        raise IdempotencyUnsupported(
            f"job store create_job_idempotent: platform-api has no "
            f"/v1/internal/jobs/create-idempotent (status={resp.status_code}) — "
            f"it predates BRREF2; no job created"
        )
    raise _unexpected("create_job_idempotent", resp.status_code, resp.json)


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
    job". ``cancel_requested`` (BR10b) is absent from a platform-api that
    predates it; the route reads absent as "no wish recorded"."""
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


class CancelRouteMissing(RuntimeError):
    """platform-api predates FK25B (no /cancel-state or /finish-cancelled).
    A running job then cannot be stopped from outside: the caller keeps the
    BR10b behaviour (409, wish recorded) and says so — never a guessed answer."""


async def cancel_state(job_id: str) -> Optional[Dict[str, Any]]:
    """store.cancel_state over the platform seam (FK25B). None = unknown id.
    Raises CancelRouteMissing behind a platform-api without the route."""
    try:
        resp = await call_platform(
            "GET", f"/v1/internal/jobs/{job_id}/cancel-state", timeout_s=_READ_TIMEOUT_S
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("cancel_state", e)
        return await store.cancel_state(job_id)
    if resp.status_code in (404, 405):
        raise CancelRouteMissing(
            f"job store cancel_state: platform-api has no cancel-state route "
            f"(HTTP {resp.status_code}) — deployed before FK25B"
        )
    if resp.status_code == 200 and isinstance(resp.json, dict) and "job" in resp.json:
        job = resp.json["job"]
        if job is None:
            return None
        if isinstance(job, dict) and "status" in job and "cancel_requested" in job:
            return job
    raise _unexpected("cancel_state", resp.status_code, resp.json)


async def finish_cancelled(job_id: str) -> bool:
    """store.finish_cancelled over the platform seam (FK25B). Raises
    CancelRouteMissing behind a platform-api without the route."""
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/finish-cancelled",
            timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("finish_cancelled", e)
        return await store.finish_cancelled(job_id)
    if resp.status_code in (404, 405):
        raise CancelRouteMissing(
            f"job store finish_cancelled: platform-api has no finish-cancelled "
            f"route (HTTP {resp.status_code}) — deployed before FK25B"
        )
    if (
        resp.status_code == 200
        and isinstance(resp.json, dict)
        and isinstance(resp.json.get("changed"), bool)
    ):
        return resp.json["changed"]
    raise _unexpected("finish_cancelled", resp.status_code, resp.json)


async def heartbeat(job_id: str) -> None:
    """Raises JobRowMissing (counted, logged) when the row is gone."""
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/heartbeat", timeout_s=_READ_TIMEOUT_S
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("heartbeat", e)
        try:
            return await store.heartbeat(job_id)
        except store.JobRowMissing as missing:
            raise _row_missing_error("heartbeat", job_id, missing)
    if _is_row_missing(resp):
        raise _row_missing_error("heartbeat", job_id)
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
    """Raises JobRowMissing (counted, logged) when the row is gone. A
    platform-api from before BR11 answers 204 either way — degraded to the old
    silence, not worse."""
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/done",
            json={"result": result}, timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("mark_done", e)
        try:
            return await store.mark_done(job_id, result)
        except store.JobRowMissing as missing:
            raise _row_missing_error("mark_done", job_id, missing)
    if _is_row_missing(resp):
        raise _row_missing_error("mark_done", job_id)
    if resp.status_code != 204:
        raise _unexpected("mark_done", resp.status_code, resp.json)


async def mark_error(
    job_id: str,
    message: str,
    code: Optional[str] = None,
    retryable: Optional[bool] = None,
    retry_after_s: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
    teilantwort: Optional[str] = None,
    diagnose_ref: Optional[str] = None,
    **usage_felder: Any,
) -> None:
    # usage_felder: usage / usage_fehlt of a failed research self-call
    # (error_contract.job_usage_fields). A platform-api from before RUSAGE
    # drops them like the BR9 fields below — the job error loses the usage,
    # nothing else.
    # A platform-api from before JOBENDE drops `details` the same way.
    # A platform-api from before BR9 ignores retryable/retry_after_s (pydantic
    # drops unknown fields) and stores {message, code}; GET then derives the
    # verdict from the code (error_contract.job_code_fields). Degraded, not wrong
    # for any code except a research/transport failure, which reads as final.
    unbekannt = set(usage_felder) - {"usage", "usage_fehlt"}
    if unbekannt:
        # Same rule as store.mark_error: a field outside the contract is a
        # caller bug, not something to send and hope a platform-api keeps.
        raise TypeError(f"mark_error: unknown fields {sorted(unbekannt)}")
    body: Dict[str, Any] = {"message": message, "code": code,
                            "retryable": retryable, "retry_after_s": retry_after_s,
                            **usage_felder}
    if details:
        body["details"] = details
    # teilantwort / diagnose_ref (error_contract.JOB_ERROR_CAUSE_EXTRA_KEYS,
    # BRTRUNC2) flat and only when set.
    extra = {name: value for name, value in (
        ("teilantwort", teilantwort), ("diagnose_ref", diagnose_ref)) if value is not None}
    body.update(extra)
    # Every cause field this write carries. The platform-api names the ones
    # it stored when asked (ursache_bestaetigen, BRTRUNC2N); a platform-api
    # from before that answers 204 without a list, and the check below makes
    # the gap loud instead of trusting it. Without the flag a current
    # platform-api answers 204 as before: workers from before BRTRUNC2N
    # accept nothing else.
    cause = {**(details or {}), **{k: usage_felder[k] for k in usage_felder}, **extra}
    if cause:
        body["ursache_bestaetigen"] = True
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/error",
            json=body,
            timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("mark_error", e)
        try:
            return await store.mark_error(
                job_id, message, code=code,
                retryable=retryable, retry_after_s=retry_after_s,
                details=details, teilantwort=teilantwort, diagnose_ref=diagnose_ref,
                **usage_felder,
            )
        except store.JobRowMissing as missing:
            raise _row_missing_error("mark_error", job_id, missing)
    if _is_row_missing(resp):
        raise _row_missing_error("mark_error", job_id)
    if resp.status_code not in (200, 204):
        raise _unexpected("mark_error", resp.status_code, resp.json)
    if not cause:
        return
    echo = resp.json if resp.status_code == 200 and isinstance(resp.json, dict) else {}
    if echo.get("zeile_unveraendert"):
        # The row was already closed ('cancelled', FK25BR R8 P2-a): nothing
        # of this write was stored, on purpose — not a contract gap.
        logger.warning(
            "job store mark_error: job %s was already %s, its error and cause "
            "fields %s were not stored", job_id, echo["zeile_unveraendert"], sorted(cause),
        )
        return
    stored = echo.get(JOB_ERROR_CAUSE_ECHO_KEY)
    stored = stored if isinstance(stored, list) else []
    dropped = [name for name in cause if name not in stored]
    if dropped:
        await _report_dropped_cause(job_id, body, cause, dropped, resp.status_code)


async def _report_dropped_cause(
    job_id: str,
    body: Dict[str, Any],
    cause: Dict[str, Any],
    dropped: List[str],
    status: int,
) -> None:
    """The platform-api stored the error but did not confirm its cause fields:
    it predates BRTRUNC2N (204 without echo). It may have kept some of them
    (one from JOBENDE keeps details and usage) or none — it does not say.
    Deploy order is platform-api first, then the workers (server2 before the
    worker host).

    Loud at the job, not only in the log: the error is written once more with
    the same fields, its message naming the unconfirmed fields and their
    short values. The old route updates the row by job_id regardless of
    status, so the second write lands and keeps whatever it kept the first
    time."""
    for name in dropped:
        _count(_cause_dropped, name)
    logger.error(
        "💀 job store mark_error: platform-api (status=%s) did not confirm the "
        "cause fields %s of job %s — it predates BRTRUNC2N and may have dropped "
        "them. Deploy platform-api before the workers.",
        status, dropped, job_id,
    )
    kurz = ", ".join(
        f"{name}={cause[name]}" for name in dropped
        if name not in ("teilantwort", "usage")
    )
    note = (
        f" [bridge-vertrag: platform-api ohne BRTRUNC2N hat {', '.join(dropped)} "
        f"nicht bestaetigt{'; ' + kurz if kurz else ''}]"
    )
    try:
        resp = await call_platform(
            "POST", f"/v1/internal/jobs/{job_id}/error",
            json={**{k: v for k, v in body.items() if k != "ursache_bestaetigen"},
                  "message": f"{body['message']}{note}"},
            timeout_s=_WRITE_TIMEOUT_S,
        )
        if resp.status_code != 204:
            raise _unexpected("mark_error (vertrag)", resp.status_code, resp.json)
    except Exception as e:
        # The error itself is stored; only the note is missing. Said here,
        # once more, at error level — the run must still end (registry).
        logger.error(
            "💀 job store mark_error: note about the dropped cause fields of job "
            "%s could not be written: %s", job_id, e,
        )


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


async def prune_jobs(terminal_ttl_seconds: int, max_age_seconds: int) -> Dict[str, Any]:
    """Retention pass (store.prune_jobs). There is deliberately no worker-side
    cleanup_old any more: its route on a platform-api from before BR11 deletes
    running and parked jobs (BR10R M-B). 404/405 here = that old platform-api
    → RetentionRouteMissing, and nothing is deleted."""
    try:
        resp = await call_platform(
            "POST", "/v1/internal/jobs-maintenance/prune",
            json={
                "terminal_ttl_seconds": terminal_ttl_seconds,
                "max_age_seconds": max_age_seconds,
            },
            timeout_s=_WRITE_TIMEOUT_S,
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("prune_jobs", e)
        return await store.prune_jobs(terminal_ttl_seconds, max_age_seconds)
    if resp.status_code in (404, 405):
        _count(_retention, "prune_route_missing")
        raise RetentionRouteMissing(
            f"platform-api has no POST /v1/internal/jobs-maintenance/prune "
            f"(status {resp.status_code}) — it predates BR11; no job is deleted "
            f"until it is updated"
        )
    if (
        resp.status_code == 200 and isinstance(resp.json, dict)
        and isinstance(resp.json.get("removed"), int)
        and isinstance(resp.json.get("expired"), list)
        and isinstance(resp.json.get("running_over_max_age"), int)
    ):
        return resp.json
    raise _unexpected("prune_jobs", resp.status_code, resp.json)


async def list_jobs(
    *,
    app_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    viewer_owner: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """viewer_owner (IDEMR R2): keyed jobs only for this owner. A platform-api
    that does not confirm the filter (keyed_owner_filter) predates it and may
    have listed foreign keyed jobs — refused loudly, never passed on."""
    params: Dict[str, Any] = {"limit": limit}
    if app_id is not None:
        params["app_id"] = app_id
    if user_id is not None:
        params["user_id"] = user_id
    if status is not None:
        params["status"] = status
    if viewer_owner is not None:
        params["viewer_owner"] = viewer_owner
    try:
        resp = await call_platform(
            "GET", "/v1/internal/jobs", params=params, timeout_s=_READ_TIMEOUT_S
        )
    except PlatformUnavailable as e:
        _fallback_or_raise("list_jobs", e)
        return await store.list_jobs(
            app_id=app_id, user_id=user_id, status=status, limit=limit,
            viewer_owner=viewer_owner,
        )
    if resp.status_code == 400:
        # The internal route maps the store's ValueError to 400 — re-raise as
        # the same type so both stages present one contract to routes.py.
        detail = (resp.json or {}).get("detail") if isinstance(resp.json, dict) else None
        raise ValueError(detail or "invalid list_jobs arguments")
    body = resp.json if isinstance(resp.json, dict) else {}
    if resp.status_code == 200 and isinstance(body.get("jobs"), list):
        if body.get("keyed_owner_filter") is not True:
            raise IdempotencyUnsupported(
                "job store list_jobs: platform-api did not confirm the keyed-job "
                "owner filter — it predates IDEMR R2; listing refused"
            )
        return body["jobs"]
    raise _unexpected("list_jobs", resp.status_code, resp.json)
