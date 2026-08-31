"""Generic async-job HTTP surface — additive, feature-flagged.

    POST /v1/jobs           → { job_id, status:'pending', kind }   (returns in <1s)
    GET  /v1/jobs/{job_id}   → { status, elapsed_seconds, progress, result?, error? }

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
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from src.auth import security, verify_api_key
from src.jobs import store, store_client
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
    short-circuits to False, so the veto behaves exactly as before.
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
        logger.warning(
            f"🔒 job submission rejected: worker {_worker_id} capacity-locked "
            f"({retry_after}s remaining) — nginx retries on next worker"
        )
        return account_exhausted_error(retry_after_s=retry_after)

    job_id = "job_" + uuid.uuid4().hex
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
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status filter '{status}'. Valid: done, error, pending, running",
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


@router.get("/v1/jobs/{job_id}")
async def get_job_endpoint(
    job_id: str,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Poll a job. 404 = unknown id or expired (TTL cleanup). Terminal states carry
    `result` (done) or `error` (error)."""
    await verify_api_key(request, credentials)
    _require_enabled()

    job = await store_client.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"Async job not found (unknown id, or expired): {job_id}",
        )

    elapsed = None
    created = job.get("created_at")
    if isinstance(created, datetime):
        terminal = job["status"] in (store.JOB_STATUS_DONE, store.JOB_STATUS_ERROR)
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
        "error": job.get("error") if job["status"] == store.JOB_STATUS_ERROR else None,
    }
