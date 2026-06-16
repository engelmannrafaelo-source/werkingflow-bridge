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
import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from src.auth import security, verify_api_key
from src.db.client import is_db_enabled
from src.jobs import store
from src.jobs.registry import get_executor, registered_kinds, run_generic_job

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
    await store.create_job(job_id, body.kind, body.payload, attribution)
    asyncio.create_task(run_generic_job(job_id, body.kind, body.payload, attribution))
    logger.info(f"📨 Async job {job_id} dispatched (kind={body.kind})")

    return {"job_id": job_id, "status": "pending", "kind": body.kind}


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
