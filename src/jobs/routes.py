"""Generic async-job HTTP surface — additive, feature-flagged.

    POST /v1/jobs           → { job_id, status:'pending', kind }   (returns in <1s)
    GET  /v1/jobs/{job_id}   → { status, elapsed_seconds, progress, result?, error? }
    DELETE /v1/jobs/{job_id} → withdraw a job no worker has started yet (BR10)

Inert unless BRIDGE_GENERIC_JOBS_ENABLED=true (503 otherwise) AND a job store is
reachable — platform-api (BRIDGE_SERVICE_TOKEN, ADR-0009 Weg b) or the direct
Postgres connection (BRIDGE_DB_URL); see src.jobs.store_client for the staging.
Existing endpoints (incl. /v1/research async) are untouched.

main.py wires this router (include_router), registers executors (register_executor),
and injects its canonical attribution extractor (set_attribution_extractor) so we
reuse the same X-* header parsing/billing as every other endpoint without an import
cycle (main.py imports this module, not the other way around).
"""
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from src.auth import security, verify_api_key
from src.error_contract import job_error_view
from src.middleware.bridge_error import (
    REASON_JOB_ALREADY_RUNNING,
    REASON_JOB_NOT_FOUND,
    REASON_JOB_TERMINAL,
    job_cancel_error,
    job_home_unconfigured_error,
    job_id_malformed_error,
    job_misdirected_error,
)
from src.jobs import store, store_client
from src.jobs.job_id import (
    JobHomeUnconfigured,
    JobIdMalformed,
    home_bridge_id,
    new_job_id,
    parse_home,
)
from src.jobs.registry import get_executor, registered_kinds, run_generic_job, spawn

logger = logging.getLogger(__name__)
router = APIRouter()

# Injected by main.py at startup (canonical X-* header → attribution dict).
_attribution_extractor: Optional[Callable[[Request], Dict[str, Any]]] = None


def set_attribution_extractor(fn: Callable[[Request], Dict[str, Any]]) -> None:
    global _attribution_extractor
    _attribution_extractor = fn


def _generic_jobs_enabled() -> bool:
    return os.getenv("BRIDGE_GENERIC_JOBS_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _require_enabled() -> None:
    if not _generic_jobs_enabled():
        raise HTTPException(
            status_code=503,
            detail="Generic async jobs disabled (set BRIDGE_GENERIC_JOBS_ENABLED=true)",
        )
    if not store_client.is_store_available():
        raise HTTPException(
            status_code=503,
            detail=(
                "Generic async jobs require a reachable job store — neither "
                "platform-api (BRIDGE_SERVICE_TOKEN) nor a direct Postgres "
                "connection (BRIDGE_DB_URL) is configured"
            ),
        )


class JobCreateRequest(BaseModel):
    kind: str
    payload: Dict[str, Any] = {}
    # Optional explicit attribution; if omitted, derived from request headers.
    attribution: Optional[Dict[str, Any]] = None


async def _job_runs_off_pool(
    body: "JobCreateRequest", attribution: Optional[Dict[str, Any]]
) -> bool:
    """True iff this job's LLM work provably runs OFF the subscription pool.

    Only the app can answer this: it needs the job kind (request body) and, for
    research, the caller's provider pin (database) — neither is visible to
    nginx. A research job self-POSTs /v1/research (executors.research_executor)
    and inherits that endpoint's pool-vs-cloud routing, so a capacity-locked
    worker CAN still serve it. Vetoing it here would rebuild, one layer down,
    exactly the gate that made the research-cloud overflow unreachable in the
    state it exists for.

    Conservative by construction: anything not provably off-pool returns False
    and keeps the capacity veto. That deliberately includes globally
    Bedrock-pinned users (whose research also takes the cloud path via an
    implicit pin) — resolving that needs the full provider-override chain from
    the research handler, and duplicating it here would be a drift risk for a
    strictly smaller win than the correctness it buys. Status quo for them, no
    regression.

    Inert while RESEARCH_CLOUD_ENABLED is off: resolve_research_cloud_routing
    returns False, so the veto behaves exactly as before. The one exception is
    a user explicitly pinned to the cloud lane while it is switched off — that
    raises (ResearchCloudDisabledError) rather than routing them to the pool,
    and is caught below like any other probe failure: the veto stays, and the
    caller meets the same refusal again inside the job, where it belongs.
    """
    if body.kind != "research":
        return False
    try:
        from src.research_cloud.routing import resolve_research_cloud_routing

        # Same inputs the executor's self-call will carry (it forwards
        # attribution.user_id as X-User-ID), so this decision and the one the
        # research handler makes inside the job agree by construction.
        user_id = (attribution or {}).get("user_id")
        payload = body.payload or {}
        return await resolve_research_cloud_routing(
            user_id, bool(payload.get("cloud_overflow"))
        )
    except Exception as exc:
        # A failed probe must never widen admission — keep the veto, say why.
        logger.warning(
            f"job placement: research-cloud routing probe failed, keeping the "
            f"capacity veto: {exc}"
        )
        return False


@router.post("/v1/jobs")
async def create_job_endpoint(
    body: JobCreateRequest,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Dispatch a job and return immediately. The work runs off-request (here on
    the Bridge), so the caller never holds a long connection — poll GET /v1/jobs/{id}."""
    await verify_api_key(request, credentials)
    if body.kind == "erkunder":
        from pydantic import ValidationError

        from src.erkunder.models import Auftrag
        from src.erkunder.zugang import erkunder_schluessel_erlaubt

        erkunder_schluessel_erlaubt(request, credentials)
        try:
            Auftrag.model_validate(body.payload)
        except ValidationError:
            raise HTTPException(400, "ungueltiger Erkunder-Auftrag") from None
    _require_enabled()

    if get_executor(body.kind) is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown job kind '{body.kind}'. Registered: {registered_kinds()}",
        )

    attribution = body.attribution
    if attribution is None and _attribution_extractor is not None:
        attribution = _attribution_extractor(request)

    # ADR-0011: the job's HOME bridge comes from the LB-stamped request
    # context, NEVER from the body — a body-supplied bridge_origin would let a
    # caller redirect whose budget pays. Persisted with the job so a reclaim
    # after restart (and the executor's self-call) keeps billing at home.
    from src.federation import get_request_origin
    _origin = get_request_origin()
    if _origin:
        attribution = dict(attribution or {})
        attribution["bridge_origin"] = _origin
    elif attribution and "bridge_origin" in attribution:
        attribution = {k: v for k, v in attribution.items() if k != "bridge_origin"}

    # Placement veto (Autobahn): the job EXECUTES on THIS worker (spawn below),
    # and the executor's chat self-call pins to localhost — the LLM work can
    # only ever use THIS worker's account. If that account is capacity-locked
    # (weekly/session window — Anthropic told us when to retry), accepting the
    # job would create a row that can only die with account_exhausted after
    # minutes of doomed in-process retries. Reject SYNCHRONOUSLY with the same
    # 429 envelope the chat endpoint emits; nginx's /v1/jobs location retries
    # the POST on the next worker (proxy_next_upstream http_429), so placement
    # migrates to an account with capacity. Nothing is persisted before this
    # check — the reject is retry-safe by construction. (Root-caused
    # 2026-07-29: energy harmonize jobs landed round-robin on weekly-locked
    # workers and died with UPSTREAM_HTTP_429 wrapped in job errors.)
    from src.middleware.capacity_lock import get_capacity_lock
    from src.middleware.bridge_error import account_exhausted_error

    _worker_id = os.getenv("INSTANCE_NAME", "unknown")
    _cap_lock = get_capacity_lock()
    if _cap_lock.is_locked(_worker_id) and not await _job_runs_off_pool(body, attribution):
        retry_after = max(60, _cap_lock.remaining_s(_worker_id))
        # Name the window that actually ran out (session vs weekly). The lock
        # recorded it when it was set; passing it on is what keeps the 429 from
        # telling the caller to wait days for a weekly reset when the real wait
        # is minutes (see bridge_error.account_exhausted_error).
        _lock_info = _cap_lock.get_lock_info(_worker_id) or {}
        logger.warning(
            f"🔒 job submission rejected: worker {_worker_id} capacity-locked "
            f"({retry_after}s remaining, reason={_lock_info.get('reason') or 'unknown'}) "
            f"— nginx retries on next worker"
        )
        return account_exhausted_error(
            retry_after_s=retry_after, limit_window=_lock_info.get("reason")
        )

    # ADR-0012: the id names the bridge whose store will hold this row, so the
    # later GET can be routed to it without anybody keeping state. Fail CLOSED
    # when this bridge cannot name itself — an untagged id would be accepted
    # here and then be unfindable from the peer bridge, which is the exact
    # silent failure this replaces.
    try:
        job_id = new_job_id()
    except JobHomeUnconfigured as e:
        logger.error("job submission refused — job home unconfigured: %s", e)
        return job_home_unconfigured_error(str(e))

    # Persist FIRST (durable 'pending'), then dispatch. If this worker dies before
    # the task runs, the row survives at 'pending' and the watchdog requeues it
    # from any worker — the dispatch is never a silent fire-and-forget loss.
    await store_client.create_job(job_id, body.kind, body.payload, attribution)
    spawn(run_generic_job(job_id, body.kind, body.payload, attribution))
    logger.info(f"📨 Async job {job_id} dispatched (kind={body.kind})")

    return {"job_id": job_id, "status": "pending", "kind": body.kind}


@router.get("/v1/jobs")
async def list_jobs_endpoint(
    request: Request,
    app_id: Optional[str] = Query(default=None),
    user_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """List jobs scoped to the calling app/user. At least one of app_id or
    user_id is required (fail loud otherwise — no unscoped listing).

    Attribution scope check: the caller may only list jobs matching their own
    X-App-ID / X-User-ID headers. Requesting a different app_id or user_id
    than the one in the request headers raises 403 (fail loud, no cross-scope
    leak). Callers with no attribution headers may still filter by the
    explicit query params they provide (service-to-service use-case where
    headers are absent but the filter is unambiguous)."""
    await verify_api_key(request, credentials)
    _require_enabled()

    if app_id is None and user_id is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of app_id or user_id query parameters is required",
        )

    if status is not None and status not in (
        store.JOB_STATUS_PENDING,
        store.JOB_STATUS_RUNNING,
        store.JOB_STATUS_DONE,
        store.JOB_STATUS_ERROR,
        store.JOB_STATUS_CANCELLED,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid status filter '{status}'. "
                f"Valid: cancelled, done, error, pending, running"
            ),
        )

    # Attribution scope guard: if the caller sends attribution headers, the
    # filter params MUST match — prevents app-A from listing app-B's jobs.
    if _attribution_extractor is not None:
        caller_attr = _attribution_extractor(request)
        caller_app = caller_attr.get("app_id")
        caller_user = caller_attr.get("user_id")
        if app_id is not None and caller_app and caller_app != app_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"app_id filter '{app_id}' does not match "
                    f"caller attribution '{caller_app}'"
                ),
            )
        if user_id is not None and caller_user and caller_user != user_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"user_id filter '{user_id}' does not match "
                    f"caller attribution '{caller_user}'"
                ),
            )

    jobs = await store_client.list_jobs(app_id=app_id, user_id=user_id, status=status, limit=limit)
    return {"jobs": jobs}


def _reject_foreign_or_malformed(job_id: str) -> Optional[JSONResponse]:
    """Guard for every single-job lookup (ADR-0012).

    Returns the response to send, or None when this bridge may answer.

    Three outcomes, all of them loud where today there was one silent 404:

      * malformed id            → 400, non-retryable. Not a job we lost — not
                                  a job id at all.
      * id names another bridge → 421 misdirected. The job is very likely
                                  alive over there; the LB was supposed to
                                  route this poll to it. Reaching here means it
                                  did not (LB not yet deployed, an id naming a
                                  bridge that does not exist, or the one-hop
                                  loop guard stopping a second forward).
      * this bridge cannot name itself → 503, deploy error, fail closed.

    TRANSITION (bounded, and the only tolerated softness here): an id from
    before ADR-0012 carries no marker at all, so it cannot be routed by
    anything. It keeps the pre-ADR behaviour — answered locally — with a
    warning per lookup. That window closes on its own: the store's TTL cleanup
    removes every pre-deploy row within ~an hour, after which such an id can
    only come from a stale client. Do NOT turn this into a permanent fallback;
    an unmarked id in a week's logs is a finding, not noise.
    """
    try:
        job_home = parse_home(job_id)
    except JobIdMalformed as e:
        logger.warning("job poll rejected — %s", e)
        return job_id_malformed_error(job_id, str(e))

    if job_home is None:
        logger.warning(
            "job poll for LEGACY (unmarked) id %s — answering from the local "
            "store like before ADR-0012. Expected only during the rollout "
            "window; the store TTL removes pre-deploy rows within ~1h.",
            job_id,
        )
        return None

    try:
        own = home_bridge_id()
    except JobHomeUnconfigured as e:
        logger.error("job poll cannot be answered — job home unconfigured: %s", e)
        return job_home_unconfigured_error(str(e))

    if job_home != own:
        logger.error(
            "job poll MISDIRECTED: %s belongs to bridge %r, this is %r — the "
            "load balancer did not route by the id marker (ADR-0012)",
            job_id, job_home, own,
        )
        return job_misdirected_error(job_id, job_home, own)

    return None


@router.get("/v1/jobs/{job_id}")
async def get_job_endpoint(
    job_id: str,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Poll a job. 404 = unknown id or expired (TTL cleanup). Terminal states carry
    `result` (done) or `error` (error).

    ADR-0012 — before that 404 may be used, the id has to be established as one
    this bridge could possibly hold. 404 means "gone"; a job on the other
    bridge is not gone, and a typo is not a job at all. Both get their own
    loud answer (400 / 421); only a genuine miss on OUR store keeps the 404."""
    await verify_api_key(request, credentials)
    _require_enabled()

    guard = _reject_foreign_or_malformed(job_id)
    if guard is not None:
        return guard

    job = await store_client.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"Async job not found (unknown id, or expired): {job_id}",
        )

    elapsed = None
    created = job.get("created_at")
    if isinstance(created, datetime):
        terminal = job["status"] in store.JOB_TERMINAL_STATUSES
        end = job.get("updated_at") if terminal else datetime.now(timezone.utc)
        if isinstance(end, datetime):
            elapsed = round((end - created).total_seconds(), 2)

    return {
        "job_id": job_id,
        "kind": job["kind"],
        "status": job["status"],
        "elapsed_seconds": elapsed,
        "progress": job.get("progress"),
        "result": job.get("result") if job["status"] == store.JOB_STATUS_DONE else None,
        # Always carries retryable/retry_after_s (src/error_contract.py).
        "error": (
            job_error_view(job.get("error"))
            if job["status"] == store.JOB_STATUS_ERROR else None
        ),
        **_deferral_view(job),
    }


# The two attribution dimensions that say whose job it is. The rest (agent,
# session, workflow, bridge_origin) describe the call, not the owner, and may
# legitimately differ between the submit and the cancel.
_OWNER_KEYS = ("app_id", "user_id")


def _caller_owns(job: Dict[str, Any], request: Request) -> bool:
    """Does the caller of this request own `job`? (BR10)

    Owner = the job's stored attribution (app_id, user_id), which the submit
    took from the caller's X-* headers unless the body named it explicitly.
    The caller is read with the SAME extractor, so a client that cancels with
    the headers it submitted with matches by construction; an absent value
    matches only an absent value. The job id itself is not proof of
    ownership — GET treats it as a capability, a write must not.

    No extractor wired (main.py always wires one) would make everybody an
    owner of nothing or of everything — fail closed and loud instead."""
    if _attribution_extractor is None:
        raise RuntimeError(
            "DELETE /v1/jobs/{id}: no attribution extractor wired "
            "(main.py set_attribution_extractor) — cannot establish the owner"
        )
    caller = _attribution_extractor(request) or {}
    owner = job.get("attribution") or {}
    return all((caller.get(k) or None) == (owner.get(k) or None) for k in _OWNER_KEYS)


def _job_not_found(job_id: str) -> JSONResponse:
    """The one answer for "unknown, expired, or not yours" — built from the
    request's id alone, so a foreign job cannot be told from a missing one."""
    return job_cancel_error(
        job_id, REASON_JOB_NOT_FOUND, 404,
        f"Async job not found (unknown id, or expired): {job_id}",
    )


@router.delete("/v1/jobs/{job_id}")
async def cancel_job_endpoint(
    job_id: str,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Withdraw a job that no worker has started yet (BR10).

    Its owner gives up waiting (a caller's deadline) — without this, a job
    still 'pending' (fresh, or parked with deferred_until) would be started
    later anyway and billed for a result nobody reads.

      200 {job_id, status:'cancelled'}  pending/deferred → cancelled, or it
                                        already was cancelled (idempotent)
      409 job_already_running           a worker has it (also when it won the
                                        race against this call by a hair)
      409 job_terminal                  done/error; error.status names which
      404 job_not_found                 unknown, expired, or someone else's
      400/421/503                       the same id guard as GET (ADR-0012)

    Exactly one store read precedes every 404, owned or not, so the timing
    carries no existence signal either. The cancel itself is one atomic store
    call (store.cancel_job, row lock) — the route never writes on a status it
    read earlier."""
    await verify_api_key(request, credentials)
    _require_enabled()

    guard = _reject_foreign_or_malformed(job_id)
    if guard is not None:
        return guard

    job = await store_client.get_job(job_id)
    if not job:
        return _job_not_found(job_id)
    if not _caller_owns(job, request):
        # Server-side only: the wire answer is the plain not-found above.
        logger.warning(
            "job cancel refused — caller is not the owner of %s (answered 404)", job_id
        )
        return _job_not_found(job_id)

    outcome = await store_client.cancel_job(job_id)
    if outcome is None:
        # Removed between the read and the cancel (TTL cleanup) — it is gone.
        return _job_not_found(job_id)

    status = outcome["status"]
    if status == store.JOB_STATUS_CANCELLED:
        if outcome["changed"]:
            logger.info(
                f"🛑 Async job {job_id} (kind={job.get('kind')}) cancelled by its owner"
            )
        return {"job_id": job_id, "status": store.JOB_STATUS_CANCELLED}
    if status == store.JOB_STATUS_RUNNING:
        return job_cancel_error(
            job_id, REASON_JOB_ALREADY_RUNNING, 409,
            f"Async job {job_id} is already running and cannot be cancelled",
            job_status=status,
        )
    if status in store.JOB_TERMINAL_STATUSES:
        return job_cancel_error(
            job_id, REASON_JOB_TERMINAL, 409,
            f"Async job {job_id} has already finished ({status}) "
            f"and cannot be cancelled",
            job_status=status,
        )
    # A status this code does not know is a contract break, not a datum.
    raise RuntimeError(f"job cancel {job_id}: store returned unknown status {status!r}")


def _deferral_view(job: Dict[str, Any]) -> Dict[str, Any]:
    """Why a 'pending' job is not running (BR9). `deferred_until` in the
    future = the bridge parked it on purpose (a dependency or no account
    capacity) and restarts it by itself then; a pending job without it that
    has been pending for long is one that nobody has picked up. Status stays
    'pending' so existing pollers keep working."""
    count = job.get("defer_count") or 0
    if job["status"] != store.JOB_STATUS_PENDING:
        # The columns keep the last wait after the job was claimed again;
        # only the count (how often it waited) is still true then.
        return {"deferred_until": None, "defer_count": count, "defer_reason": None}
    until = job.get("deferred_until")
    if isinstance(until, datetime):
        until = until.isoformat()
    return {
        "deferred_until": until,
        "defer_count": count,
        "defer_reason": job.get("defer_reason") if until else None,
    }
