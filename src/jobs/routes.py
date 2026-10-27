"""Generic async-job HTTP surface — additive, feature-flagged.

    POST /v1/jobs           → { job_id, status:'pending', kind }   (returns in <1s)
                              with Idempotency-Key: 201 new / 200 replay (BRREF2)
    GET  /v1/jobs/{job_id}   → { status, elapsed_seconds, progress, result?, error? }
    DELETE /v1/jobs/{job_id} → withdraw a job no worker has started yet (BR10),
                              or stop a running chat/research/doc-agent job (FK25B)

Inert unless BRIDGE_GENERIC_JOBS_ENABLED=true (503 otherwise) AND a job store is
reachable — platform-api (BRIDGE_SERVICE_TOKEN, ADR-0009 Weg b) or the direct
Postgres connection (BRIDGE_DB_URL); see src.jobs.store_client for the staging.
Existing endpoints (incl. /v1/research async) are untouched.

main.py wires this router (include_router), registers executors (register_executor),
and injects its canonical attribution extractor (set_attribution_extractor) so we
reuse the same X-* header parsing/billing as every other endpoint without an import
cycle (main.py imports this module, not the other way around).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from src.auth import security, verify_api_key
from src.config import config
from src.error_contract import JOB_CODE_STORE_INCOMPATIBLE, job_error_view
from src.middleware.bridge_error import (
    REASON_IDEMPOTENCY_KEY_INVALID,
    REASON_IDEMPOTENCY_KEY_REUSED,
    REASON_JOB_ALREADY_RUNNING,
    REASON_JOB_CANCEL_LEDGER_FAILED,
    REASON_JOB_CANCEL_LEDGER_UNKNOWN,
    REASON_JOB_NOT_FOUND,
    REASON_JOB_STORE_INCOMPATIBLE,
    REASON_JOB_TERMINAL,
    idempotency_error,
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
from src.jobs.registry import (
    ABBRUCH_PROGRESS_KEY,
    EIGENTUEMER_KEY,
    JOB_ABBRECHBAR_IM_LAUF,
    get_executor,
    registered_kinds,
    run_generic_job,
    spawn,
)
from src.jobs.selbstaufruf import abbruch_melden
from src.lauf_abschluss import abschluss_eines_ergebnisses

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


class JobAttemptIdentity(BaseModel):
    # Opaque client identifiers, never customer names/content.
    call_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$", strict=True)
    attempt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$", strict=True)

    def key(self) -> str:
        encoded = json.dumps([self.call_id, self.attempt_id], separators=(",", ":")).encode("ascii")
        return "bridge-attempt-v1:" + hashlib.sha256(encoded).hexdigest()


class JobCreateRequest(BaseModel):
    call_id: Optional[str] = None
    attempt_id: Optional[str] = None
    kind: str
    payload: Dict[str, Any] = {}
    # Optional explicit attribution; if omitted, derived from request headers.
    attribution: Optional[Dict[str, Any]] = None


# ── Idempotency-Key (BRREF2) ─────────────────────────────────────────────
# Contract, agreed with the report side:
#   no header                      → unchanged (200, a new job every time)
#   new key                        → 201, new job, Idempotent-Replayed: false
#   same key + owner, same body    → 200, the SAME job_id, no new job, whatever
#                                    its status (pending/running/done/error/
#                                    cancelled), Idempotent-Replayed: true
#   same key + owner, other body   → 422 idempotency_key_reused, no job; the
#                                    answer names no job (IDEMR)
#   malformed key                  → 400 idempotency_key_invalid, no job
# An existing key is looked up BEFORE the capacity veto (IDEMR): a full bridge
# still answers the retry with its job. GET /v1/jobs/{id} answers a keyed job
# only to the owner that created it — anyone else gets the same 404 as for an
# unknown id. Unkeyed jobs keep GET-by-id as before.
# Every keyed answer echoes Idempotency-Key. The key lives as long as its job
# row: never deleted while pending/running, and 24 h past the terminal state
# (store.IDEMPOTENCY_RETENTION_SECONDS). Exactly-once under concurrency is the
# store's UNIQUE index (migration 065), not a check in this file. The index is
# per bridge database; that every attempt reaches the SAME bridge is the LB's
# part (docker/nginx.conf, keyed submissions: one fixed bridge, no fallback).
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
IDEMPOTENCY_KEY_MAX_LEN = 200


def _idempotency_key(request: Request) -> Optional[str]:
    """The header value, or None when absent. Raises ValueError when present
    but unusable (empty, longer than 200, or not printable ASCII)."""
    raw = request.headers.get(IDEMPOTENCY_HEADER)
    if raw is None:
        return None
    if not raw:
        raise ValueError(f"{IDEMPOTENCY_HEADER} is empty")
    if len(raw) > IDEMPOTENCY_KEY_MAX_LEN:
        raise ValueError(
            f"{IDEMPOTENCY_HEADER} is {len(raw)} characters, at most "
            f"{IDEMPOTENCY_KEY_MAX_LEN} are allowed"
        )
    if not all(0x20 <= ord(c) <= 0x7E for c in raw):
        raise ValueError(f"{IDEMPOTENCY_HEADER} must be printable ASCII")
    return raw


def _idempotency_owner(request: Request) -> str:
    """Whose key namespace this is — an opaque digest over the identity parts,
    each length-prefixed, so no choice of names can make two different
    identities collide (IDEMR: "service|app"+"report" and "service"+
    "app|report" were one owner when the parts were joined with "|").

    Parts: how the caller authenticated (principal or shared key), the
    principal's name, the app and the user. App and user come from the
    caller's X-* headers via the canonical extractor — never from the body's
    attribution, which a caller sets freely per request. Under the shared key
    the user part is what keeps alice's key from naming bob's job; it is a
    header the key holder asserts, not authenticated identity (the same limit
    _caller_owns documents). No extractor wired → fail loud."""
    if _attribution_extractor is None:
        raise RuntimeError(
            "POST /v1/jobs with Idempotency-Key: no attribution extractor wired "
            "(main.py set_attribution_extractor) — cannot establish the key owner"
        )
    principal = getattr(request.state, "principal", None)
    name = getattr(principal, "name", None)
    caller = _attribution_extractor(request) or {}
    parts = (
        "idem-owner-v1",
        "principal" if name else "shared-key",
        name or "",
        caller.get("app_id") or "",
        caller.get("user_id") or "",
    )
    h = hashlib.sha256()
    for part in parts:
        raw = str(part).encode("utf-8")
        h.update(len(raw).to_bytes(4, "big"))
        h.update(raw)
    return "v1:" + h.hexdigest()


def _visible_to_caller(job: Optional[Dict[str, Any]], request: Request) -> bool:
    """THE ownership boundary of a keyed job (IDEMR R2) — every public access
    to a single job goes through here (GET, DELETE); the list applies the same
    rule in SQL with the same owner (store.list_jobs viewer_owner).

    A job created under an Idempotency-Key (migration 065) is visible only to
    the owner that created it: its id can be learned by retrying someone
    else's key, so the id alone must open nothing. Anyone else is told what
    an unknown id is told. Unkeyed jobs: always True here — their access
    rules stay as they were (GET by id is a capability, DELETE checks
    _caller_owns)."""
    if not job:
        return False
    held = job.get("idempotency_owner")
    return not held or held == _idempotency_owner(request)


def _key_reused(key: str, owner: str, held_by: str) -> JSONResponse:
    logger.warning(
        "job submission refused — Idempotency-Key reused with a different "
        "body (owner=%s, existing job %s)", owner, held_by,
    )
    return idempotency_error(
        REASON_IDEMPOTENCY_KEY_REUSED, 422,
        f"{IDEMPOTENCY_HEADER} was already used with a different request body; "
        f"no job created",
        idempotency_key=key,
    )


def _keyed_response(
    status_code: int, body: Dict[str, Any], key: str, replayed: bool
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={
            IDEMPOTENCY_HEADER: key,
            REPLAYED_HEADER: "true" if replayed else "false",
        },
    )


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
    # Test-KI-Deckel (R4): ein Testaufruf legt nur chat/ping an, chat nur mit
    # gueltigem X-Test-Lauf; ein leerer/ungueltiger Kopf ist schon hier 403.
    from src.budget.test_ki_deckel import job_anlegen_pruefen
    _test_lauf = await job_anlegen_pruefen(request, body.kind)
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

    try:
        idem_key = _idempotency_key(request)
    except ValueError as e:
        return idempotency_error(REASON_IDEMPOTENCY_KEY_INVALID, 400, str(e))

    if config.job_attempt_identity_enabled and (
        body.call_id is not None or body.attempt_id is not None
    ):
        from pydantic import ValidationError
        try:
            identity = JobAttemptIdentity(call_id=body.call_id, attempt_id=body.attempt_id)
        except ValidationError:
            return idempotency_error(REASON_IDEMPOTENCY_KEY_INVALID, 400,
                                     "call_id and attempt_id must both be valid opaque identifiers")
        if idem_key != identity.key():
            return idempotency_error(REASON_IDEMPOTENCY_KEY_INVALID, 400,
                                     "Idempotency-Key must match call_id/attempt_id")

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

    # FK25B2: who may cancel this job, bound at submit to the caller's
    # AUTHENTICATED identity — the IDEMR owner (_idempotency_owner), never
    # a value from the body. See _caller_owns.
    attribution = {k: v for k, v in (attribution or {}).items() if k != EIGENTUEMER_KEY}
    if _attribution_extractor is not None:
        attribution[EIGENTUEMER_KEY] = _idempotency_owner(request)

    # Test-KI-Deckel (src/budget/test_ki_deckel.py): der Selbstaufruf laeuft mit
    # dem Worker-Schluessel. Der Prinzipal des ANLEGENDEN Aufrufs und dessen
    # GEPRUEFTER X-Test-Lauf werden darum hier, serverseitig, mit dem Job
    # gespeichert — nie aus dem Rumpf (ein mitgegebener Wert wird entfernt).
    from src.budget.test_ki_deckel import JOB_LAUF_KEY, JOB_PRINZIPAL_KEY
    attribution = {k: v for k, v in attribution.items() if k not in (JOB_PRINZIPAL_KEY, JOB_LAUF_KEY)}
    _principal = getattr(request.state, "principal", None)
    if _principal is not None and not getattr(_principal, "is_legacy", False):
        attribution[JOB_PRINZIPAL_KEY] = _principal.name
    if _test_lauf is not None:
        attribution[JOB_LAUF_KEY] = _test_lauf
    attribution = attribution or None

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
    # IDEMR: an existing key is answered before any capacity decision — the
    # job it names exists no matter how full this worker is.
    idem_owner = None
    if idem_key is not None:
        idem_owner = _idempotency_owner(request)
        try:
            held = await store_client.find_job_by_idempotency_key(
                idem_owner, idem_key, body.kind, body.payload
            )
        except store_client.IdempotencyKeyReused as e:
            return _key_reused(idem_key, idem_owner, e.job_id)
        except store_client.IdempotencyUnsupported as e:
            logger.error("job submission refused — %s", e)
            raise HTTPException(status_code=503, detail=str(e)) from None
        if held is not None:
            return _replay(held, idem_key)

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

    if idem_key is not None:
        return await _create_keyed_job(job_id, body, attribution, idem_key, idem_owner)

    # Persist FIRST (durable 'pending'), then dispatch. If this worker dies before
    # the task runs, the row survives at 'pending' and the watchdog requeues it
    # from any worker — the dispatch is never a silent fire-and-forget loss.
    await store_client.create_job(job_id, body.kind, body.payload, attribution)
    spawn(run_generic_job(job_id, body.kind, body.payload, attribution))
    logger.info(f"📨 Async job {job_id} dispatched (kind={body.kind})")

    return {"job_id": job_id, "status": "pending", "kind": body.kind}


async def _create_keyed_job(
    job_id: str,
    body: "JobCreateRequest",
    attribution: Optional[Dict[str, Any]],
    key: str,
    owner: str,
) -> JSONResponse:
    """Persist-then-dispatch like the unkeyed path, but through the store's
    atomic create-once (BRREF2). Only the request that created the row
    dispatches it; a replay returns the existing job and starts nothing."""
    try:
        outcome = await store_client.create_job_idempotent(
            job_id, body.kind, body.payload, attribution, owner, key
        )
    except store_client.IdempotencyKeyReused as e:
        return _key_reused(key, owner, e.job_id)
    except store_client.IdempotencyUnsupported as e:
        # A job store that cannot keep the promise gets no job at all — the
        # caller retries later; a plain create here would be the double job.
        logger.error("job submission refused — %s", e)
        raise HTTPException(status_code=503, detail=str(e)) from None

    job = outcome["job"]
    # The row this request named itself is ours even when the store reports a
    # replay: after a platform timeout the direct-DB fallback finds the row the
    # lost platform call already inserted (store_client.create_job_idempotent).
    created = bool(outcome["created"]) or job["job_id"] == job_id
    if created:
        spawn(run_generic_job(job_id, body.kind, body.payload, attribution))
        logger.info(f"📨 Async job {job_id} dispatched (kind={body.kind}, keyed)")
        return _keyed_response(
            201,
            {"job_id": job_id, "status": store.JOB_STATUS_PENDING, "kind": body.kind},
            key, replayed=False,
        )
    return _replay(job, key)


def _replay(job: Dict[str, Any], key: str) -> JSONResponse:
    logger.info(
        f"♻️ Async job {job['job_id']} replayed for Idempotency-Key "
        f"(status={job['status']}, kind={job['kind']}) — no new job"
    )
    return _keyed_response(
        200, {"job_id": job["job_id"], "status": job["status"], "kind": job["kind"]},
        key, replayed=True,
    )


@router.post("/v1/jobs/lookup")
async def lookup_job_endpoint(
    body: JobAttemptIdentity,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Read an attempt identity without submitting work or requiring its payload.

    A miss is UNKNOWN, never proof of nonacceptance (retention/in-flight POST).
    The required header also pins the LB to the same store as keyed submit.
    """
    await verify_api_key(request, credentials)
    if not config.job_attempt_identity_enabled:
        return JSONResponse(status_code=404, content={
            "reason": "feature_disabled", "outcome": "unknown", "job_id": None,
        }, headers={"Cache-Control": "no-store"})
    _require_enabled()
    try:
        key = _idempotency_key(request)
    except ValueError as exc:
        return idempotency_error(REASON_IDEMPOTENCY_KEY_INVALID, 400, str(exc))
    if key != body.key():
        return idempotency_error(REASON_IDEMPOTENCY_KEY_INVALID, 400,
                                 "Idempotency-Key must match call_id/attempt_id")
    try:
        job = await store_client.resolve_job_identity(_idempotency_owner(request), key)
    except store_client.JobStoreUnavailable:
        raise HTTPException(503, "Job identity lookup unavailable; outcome unknown") from None
    if job is None:
        return JSONResponse(status_code=404, content={
            "reason": "job_identity_unknown", "outcome": "unknown",
            "job_id": None,
        }, headers={IDEMPOTENCY_HEADER: key, "Cache-Control": "no-store"})
    return JSONResponse(content={"job_id": job["job_id"], "kind": job["kind"],
                                 "status": job["status"]},
                        headers={IDEMPOTENCY_HEADER: key, "Cache-Control": "no-store"})


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

    # Keyed jobs only for their owner — the rule of _visible_to_caller, in SQL.
    jobs = await store_client.list_jobs(
        app_id=app_id, user_id=user_id, status=status, limit=limit,
        viewer_owner=_idempotency_owner(request),
    )
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
    if not _visible_to_caller(job, request):
        job = None
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
        # done = executed, not "a finished answer": a chat job whose model
        # stopped the answer is done with abschluss.vollstaendig false
        # (src/lauf_abschluss, BRREF). Read it before using result.
        "abschluss": (
            abschluss_eines_ergebnisses(job.get("result"))
            if job["status"] == store.JOB_STATUS_DONE else None
        ),
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
    """Does the caller of this request own `job`? (BR10, FK25B2)

    The trust boundary is the one IDEMR draws for an Idempotency-Key, so GET
    (keyed jobs, _visible_to_caller), the key and DELETE agree on who an
    owner is: _idempotency_owner — how the caller authenticated, the
    authenticated principal's name, and the app/user it states.

      * Jobs submitted since FK25B2 carry that owner, stamped at submit
        (attribution[EIGENTUEMER_KEY], set server-side, never from the
        body). With principals on (BRIDGE_PRINCIPALS_ENABLED) a principal's
        job can be cancelled by that principal only: the name comes from its
        token, so other principals and the shared (legacy) key are refused
        whatever headers they send.
      * Under the shared key there is no authenticated user: every holder is
        a trusted backend that states the user in X-* headers — the same
        boundary as IDEMR and as the LB-stamped Hop/Origin headers (trusted
        because the sender is). A shared-key holder who states another
        user's headers is, to the bridge, that user. That is the named
        limit of the shared key, not of this check; it ends with principals.
      * Rows from before FK25B2 (no stamp) are matched on app/user as
        before. Their run cannot be stopped (registry: the self-call of an
        unbound job is not marked), so for them DELETE only records the
        wish (BR10b).

    No extractor wired (main.py always wires one) would make everybody an
    owner of nothing or of everything — fail closed and loud instead."""
    if _attribution_extractor is None:
        raise RuntimeError(
            "DELETE /v1/jobs/{id}: no attribution extractor wired "
            "(main.py set_attribution_extractor) — cannot establish the owner"
        )
    owner = job.get("attribution") or {}
    stamp = owner.get(EIGENTUEMER_KEY)
    if stamp:
        return hmac.compare_digest(str(stamp), _idempotency_owner(request))
    caller = _attribution_extractor(request) or {}
    return all((caller.get(k) or None) == (owner.get(k) or None) for k in _OWNER_KEYS)


def _owner_bound(job: Dict[str, Any]) -> bool:
    """Was the owner bound at submit (FK25B2)? Only such a job's run can be
    stopped."""
    return bool((job.get("attribution") or {}).get(EIGENTUEMER_KEY))


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
    """Withdraw a job that no worker has started yet (BR10), or stop one
    that runs (FK25B).

    Its owner gives up waiting (a caller's deadline) — without this, a job
    still 'pending' (fresh, or parked with deferred_until) would be started
    later anyway and billed for a result nobody reads.

      200 {job_id, status:'cancelled'}  pending/deferred → cancelled, or it
                                        already was cancelled (idempotent);
                                        also a running chat/research/
                                        doc-agent job whose endpoint
                                        confirmed the stop within
                                        JOB_CANCEL_WAIT_S (FK25B2,
                                        _stop_running: the CLI has ended,
                                        the stop is booked)
      202 {status:'running',            such a job is not confirmed stopped
           cancel_requested:true}       yet; GET shows 'cancelled' once it
                                        is (or 'done' if it finished first)
      409 job_terminal (running kind)   it finished before the cancel took
                                        effect; the result is kept
      502 job_store_incompatible        the platform-api predates FK25B: a
                                        running job cannot be stopped
      202 {status:'cancelled',ledger:'rueckstau'} / 502 job_cancel_ledger_failed
                                        stopped, but the stop's booking is
                                        only owed in the spool / lost
                                        (_cancelled_answer, FK25BR R2)
      409 job_already_running           every OTHER kind (erkunder, ping,
                                        proxy, pdf) a worker has (also when
                                        it won the
                                        race against this call by a hair).
                                        The run is not stopped, but the wish
                                        is recorded (error.cancel_requested
                                        = true, BR10b): if the run parks
                                        itself (429 / dependency wait) or
                                        its worker dies, the job ends
                                        'cancelled' and is never started
                                        again. A run that completes ends
                                        done/error and is billed — a later
                                        GET shows which. One DELETE is
                                        enough; repeating it is harmless.
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
    if not _visible_to_caller(job, request) or not _caller_owns(job, request):
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
            # THIS call cancelled a pending job. Pending does not mean it
            # never ran: an earlier attempt may have parked or crashed
            # (FK25BR R5) — answered from the row, like every repeat.
            return await _cancelled_answer(job_id, job.get("kind"), "pending")
        # Already cancelled (a repeated or concurrent DELETE): the same answer
        # as the DELETE that stopped it — from the stored account of the stop,
        # never a bare 200 over a failed or owed booking (FK25BR R3).
        return await _cancelled_answer(job_id, job.get("kind"), "repeated")
    if status == store.JOB_STATUS_RUNNING:
        # False only behind a platform-api from before BR10b, which records
        # no wish — the body then says so instead of promising it.
        requested = bool(outcome.get("cancel_requested"))
        if requested and job.get("kind") in JOB_ABBRECHBAR_IM_LAUF and _owner_bound(job):
            return await _stop_running(job_id, job.get("kind"))
        if requested:
            logger.info(
                f"🛑 Async job {job_id} (kind={job.get('kind')}) running — "
                f"cancel wish recorded, it will not be resumed"
            )
        return job_cancel_error(
            job_id, REASON_JOB_ALREADY_RUNNING, 409,
            f"Async job {job_id} is already running and cannot be cancelled"
            + ("; it will not be resumed if it pauses" if requested else ""),
            job_status=status,
            cancel_requested=requested,
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


# FK25B: how long DELETE waits for a running job to end before it answers
# 202. Covers the endpoint's poll (selbstaufruf.JOB_CANCEL_POLL_S), the CLI's
# bounded close (up to 2 × (grace + reap), BRLZR R2) and the ledger booking:
# the row turns 'cancelled' only after all of them (FK25B2).
JOB_CANCEL_WAIT_S = float(os.getenv("BRIDGE_JOB_CANCEL_WAIT_S", "10"))
JOB_CANCEL_WAIT_TICK_S = 0.25


_CALL_UIDS_HINWEIS = (
    "per-attempt call_uids and ledger status are not recorded on the job; "
    "nothing was reconstructed or re-booked"
)


def _fruehere_versuche(
    job: Dict[str, Any], bericht_ledger: Optional[str]
) -> Optional[Dict[str, Any]]:
    """The attempts of a cancelled job BEFORE the one its current account (if
    any) covers — or None when there were none (FK25BR R5/R6).

    ``attempts`` counts every start (mark_running, watchdog claim);
    ``defer_count`` the starts that parked themselves (defer_job). A park
    proves a start, so starts = max(attempts, defer_count). The current
    account always covers the LAST attempt — it is written by the one path
    that closed the row: a stop or the watchdog (a non-parked attempt), or
    defer_job ('vom_lauf', a parked one). It is not counted again.

    Of the earlier attempts, a parked one ended on its own and booked its call
    itself; any other one ended with its runner gone (the watchdog claimed it
    back) and how it was booked is not known here: ``buchung`` 'unbekannt'.
    Unreadable counters cannot prove anything: 'unbekannt' too."""
    attempts = job.get("attempts")
    defer_count = job.get("defer_count")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or \
            not isinstance(defer_count, int) or isinstance(defer_count, bool):
        return {
            "anzahl": None, "geparkt": None, "abgebrochen_ohne_bericht": None,
            "buchung": "unbekannt", "call_uids": None,
            "hinweis": (
                f"attempts/defer_count not readable from the job store "
                f"(attempts={attempts!r}, defer_count={defer_count!r}): earlier "
                f"attempts cannot be ruled out; " + _CALL_UIDS_HINWEIS
            ),
        }
    starts = max(attempts, defer_count)
    parks = defer_count
    if bericht_ledger is not None:
        starts -= 1
        if bericht_ledger == "vom_lauf":
            parks -= 1
    starts = max(0, starts)
    parks = max(0, min(parks, starts))
    if starts == 0:
        return None
    ohne_bericht = starts - parks
    return {
        "anzahl": starts,
        "geparkt": parks,
        "abgebrochen_ohne_bericht": ohne_bericht,
        "buchung": "unbekannt" if ohne_bericht else "vom_lauf",
        "call_uids": None,
        "hinweis": (
            "parked attempts booked their own calls; "
            + (f"{ohne_bericht} attempt(s) ended with their runner gone (claimed "
               f"back by the watchdog), their booking is unknown; "
               if ohne_bericht else "")
            + _CALL_UIDS_HINWEIS
        ),
    }


# The current account's ledger state → (HTTP status, reason or None).
_BERICHT_ANTWORT = {
    None: (200, None),                       # no account: no attempt was stopped
    "gebucht": (200, None),
    "vom_lauf": (200, None),
    "rueckstau": (202, None),
    "fehlgeschlagen": (502, REASON_JOB_CANCEL_LEDGER_FAILED),
}


async def _cancelled_answer(job_id: str, kind: Optional[str], wo: str) -> JSONResponse:
    """Answer for a job that reads 'cancelled' — the same for the DELETE that
    cancelled it and for every repeated or concurrent one (FK25BR R3).

    Two inputs, judged together (FK25BR R6):

      * the current account (progress.abbruch.ledger) — written by the path
        that closed the row: the stop (gebucht / rueckstau / fehlgeschlagen),
        the watchdog ('unbekannt', also over a stop's 'offen'), defer_job
        ('vom_lauf'); none when a pending row was cancelled;
      * the earlier attempts (_fruehere_versuche) — those the account does
        not cover.

    The current account sets the base answer:

      none, gebucht, vom_lauf   200
      rueckstau                 202
      fehlgeschlagen            502 job_cancel_ledger_failed
      unbekannt, offen, other   502 job_cancel_ledger_unknown
      (malformed account)       502 job_cancel_ledger_unknown

    The earlier attempts never disappear behind it:

      none                      base answer unchanged (none + none = the
                                plain 200 of a job that provably never ran)
      only parked               base answer + fruehere_versuche (buchung
                                vom_lauf); with no account: ledger
                                'fruehere_versuche'
      unknown / unreadable      a 200 or 202 becomes 502
                                job_cancel_ledger_unknown; a 502 keeps its
                                code; fruehere_versuche always in the body

    Every 502 body: status 'cancelled' (it IS cancelled — nothing runs or is
    re-booked), ledger, fruehere_versuche. ERROR log for every unknown or
    failed booking, WARNING for parked history."""
    job = await store_client.get_job(job_id) or {}
    progress = job.get("progress")
    roh = progress.get(ABBRUCH_PROGRESS_KEY) if isinstance(progress, dict) else None
    if roh is None:
        ledger: Optional[str] = None
        bericht: Dict[str, Any] = {}
    elif isinstance(roh, dict):
        ledger = roh.get("ledger") if isinstance(roh.get("ledger"), str) else "unlesbar"
        bericht = roh
    else:
        ledger, bericht = "unlesbar", {}
    versuche = _fruehere_versuche(job, ledger)
    status, reason = _BERICHT_ANTWORT.get(ledger, (502, REASON_JOB_CANCEL_LEDGER_UNKNOWN))
    historie_unbekannt = versuche is not None and versuche["buchung"] == "unbekannt"
    if historie_unbekannt and status < 300:
        status, reason = 502, REASON_JOB_CANCEL_LEDGER_UNKNOWN

    inhalt: Dict[str, Any] = {"job_id": job_id, "status": store.JOB_STATUS_CANCELLED}
    if ledger is not None:
        inhalt["ledger"] = ledger
    elif versuche is not None:
        inhalt["ledger"] = "fruehere_versuche"
    if versuche is not None:
        inhalt["fruehere_versuche"] = versuche
    lage = (
        f"Async job {job_id} (kind={kind}) cancelled ({wo}): current account "
        f"ledger={ledger!r} (writer: {bericht.get('ledger_outcomes')}, open "
        f"deductions: {bericht.get('offene_abzuege')}), earlier attempts: {versuche}"
    )
    if reason is not None or status == 202:
        logger.error(f"🛑 {lage}")
    elif versuche is not None:
        logger.warning(f"🛑 {lage}")
    else:
        logger.info(f"🛑 {lage}")

    if reason is None:
        if status == 202:
            inhalt["message"] = (
                f"Async job {job_id} is stopped; its ledger booking did not go "
                f"through yet and is owed in the bridge's ledger spool, which "
                f"books it later. Not yet billed."
            )
        return JSONResponse(status_code=status, content=inhalt)

    if reason == REASON_JOB_CANCEL_LEDGER_FAILED:
        message = (
            f"Async job {job_id} is stopped (cancelled), but booking its usage in "
            f"the ledger failed and was not spooled (writer: "
            f"{bericht.get('ledger_outcomes')}, verbrauch: {bericht.get('verbrauch')}). "
            f"The run is over; the usage is not metered."
        )
    else:
        teile = []
        if ledger not in _BERICHT_ANTWORT:
            teile.append(
                f"the worker stopping its last attempt went away before its ledger "
                f"booking was confirmed (ledger: {ledger}; open budget deductions: "
                f"{bericht.get('offene_abzuege') or 'none known'})"
            )
        if historie_unbekannt:
            teile.append(
                f"earlier attempts ended without an account (attempts: "
                f"{versuche.get('anzahl')}, of them without account: "
                f"{versuche.get('abgebrochen_ohne_bericht')})"
            )
        message = (
            f"Async job {job_id} is cancelled, nothing runs any more, but "
            + "; ".join(teile)
            + ". Whether this usage is metered is unknown."
        )
    if historie_unbekannt and reason == REASON_JOB_CANCEL_LEDGER_FAILED:
        message += " Earlier attempts' booking is unknown as well."
    details = {k: v for k, v in inhalt.items() if k in ("ledger", "fruehere_versuche")}
    return job_cancel_error(
        job_id, reason, status, message,
        job_status=store.JOB_STATUS_CANCELLED, details=details,
    )


def _store_incompatible(job_id: str, detail: str, gestoppt: bool = False) -> JSONResponse:
    """502: the platform-api predates FK25B. Never answered as BR10b's 409 or
    as 'being stopped' — a deploy mismatch someone has to see (FK25BR 4)."""
    logger.error(f"🛑 job cancel {job_id}: job store incompatible — {detail}")
    if gestoppt:
        lage = (
            "its run was stopped and booked, but the job store cannot record "
            "'cancelled' — the job reads 'error' (JOB_STORE_INCOMPATIBLE)"
        )
        status = store.JOB_STATUS_ERROR
    else:
        lage = (
            "it runs on and cannot be stopped; the cancel wish is recorded "
            "(BR10b: the run is not resumed if it pauses)"
        )
        status = store.JOB_STATUS_RUNNING
    return job_cancel_error(
        job_id, REASON_JOB_STORE_INCOMPATIBLE, 502,
        f"Async job {job_id}: the job store (platform-api) predates FK25B "
        f"({detail}) — {lage}. Deploy platform-api together with the workers.",
        job_status=status,
        cancel_requested=True,
    )


async def _stop_running(job_id: str, kind: Optional[str]) -> JSONResponse:
    """Stop a running job whose cancel wish is recorded (FK25B, FK25B2).

      200 {status:'cancelled',          the endpoint stopped the run, the CLI
           ledger:'gebucht'}            has ended and the stop is booked; the
                                        row is cancelled
      202 {status:'cancelled',          stopped; the booking is owed in the
           ledger:'rueckstau'}          ledger spool (not yet billed)
      502 job_cancel_ledger_failed      stopped; the booking failed and is
                                        not spooled (usage not metered)
      202 {status:'running',            the wish is recorded, the run is not
           cancel_requested:true}       confirmed stopped yet; GET shows the
                                        end ('cancelled', or 'done' if it
                                        finished first)
      409 job_terminal                  it finished done/error first (billed;
                                        the result is kept)
      502 job_store_incompatible        the platform-api predates FK25B: the
                                        run cannot be stopped (wish recorded)

    Wakes the run directly when it runs on this worker; otherwise its
    endpoint sees the wish within selbstaufruf.JOB_CANCEL_POLL_S."""
    local = abbruch_melden(job_id)
    deadline = time.monotonic() + JOB_CANCEL_WAIT_S
    while True:
        try:
            state = await store_client.cancel_state(job_id)
        except store_client.CancelRouteMissing as e:
            return _store_incompatible(job_id, str(e))
        if state is None:
            return _job_not_found(job_id)
        if state["status"] != store.JOB_STATUS_RUNNING:
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(JOB_CANCEL_WAIT_TICK_S)

    status = state["status"]
    if status == store.JOB_STATUS_CANCELLED:
        return await _cancelled_answer(
            job_id, kind, "this worker" if local else "another worker"
        )
    if status == store.JOB_STATUS_RUNNING:
        logger.warning(
            f"🛑 Async job {job_id} (kind={kind}) not confirmed stopped "
            f"{JOB_CANCEL_WAIT_S:.0f}s after its cancel"
        )
        return JSONResponse(
            status_code=202,
            content={
                "job_id": job_id,
                "status": store.JOB_STATUS_RUNNING,
                "cancel_requested": True,
                "message": (
                    f"Async job {job_id} is being stopped; poll GET /v1/jobs/{job_id} "
                    f"until it reads 'cancelled' (or 'done', if it finished first)"
                ),
            },
        )
    if status == store.JOB_STATUS_ERROR:
        job = await store_client.get_job(job_id)
        code = ((job or {}).get("error") or {}).get("code")
        if code == JOB_CODE_STORE_INCOMPATIBLE:
            return _store_incompatible(job_id, "no finish-cancelled route", gestoppt=True)
    if status in store.JOB_TERMINAL_STATUSES:
        return job_cancel_error(
            job_id, REASON_JOB_TERMINAL, 409,
            f"Async job {job_id} finished ({status}) before the cancel took effect",
            job_status=status,
        )
    # 'pending' again: the run parked itself and defer_job closed it as
    # cancelled — or a status this code does not know. Neither is guessed.
    raise RuntimeError(f"job cancel {job_id}: unexpected status {status!r} while stopping")


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
