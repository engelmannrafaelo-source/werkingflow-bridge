"""Generic async-job HTTP surface — additive, feature-flagged.

    POST /v1/jobs           → { job_id, status:'pending', kind }   (returns in <1s)
    GET  /v1/jobs/{job_id}   → { status, elapsed_seconds, progress, result?, error? }

Inert unless BRIDGE_GENERIC_JOBS_ENABLED=true (503 otherwise) AND BRIDGE_DB_URL is
set (the Postgres store is required). Existing endpoints (incl. /v1/research async)
are untouched — this is purely additive so the live Bridge cannot regress.

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
from src.db.client import is_db_enabled
from src.jobs import store
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
    if not is_db_enabled():
        raise HTTPException(
            status_code=503,
            detail="Generic async jobs require BRIDGE_DB_URL (Postgres store)",
        )


class JobCreateRequest(BaseModel):
    kind: str
    payload: Dict[str, Any] = {}
    # Optional explicit attribution; if omitted, derived from request headers.
    attribution: Optional[Dict[str, Any]] = None


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

    job_id = "job_" + uuid.uuid4().hex
    # Persist FIRST (durable 'pending'), then dispatch. If this worker dies before
    # the task runs, the row survives at 'pending' and the watchdog requeues it
    # from any worker — the dispatch is never a silent fire-and-forget loss.
    await store.create_job(job_id, body.kind, body.payload, attribution)
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

    jobs = await store.list_jobs(app_id=app_id, user_id=user_id, status=status, limit=limit)
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

    job = await store.get_job(job_id)
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
