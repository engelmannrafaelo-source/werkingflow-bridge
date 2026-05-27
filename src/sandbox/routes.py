"""
Sandbox endpoints — OAuth-lease authority + usage tracking.

Mounted under /v1/sandbox — only active when BRIDGE_DB_URL is set.

Auth:
  - POST /v1/sandbox/lease-token              : require_service_token
  - POST /v1/sandbox/lease-token/:id/heartbeat: require_service_token
  - POST /v1/sandbox/lease-token/:id/release  : require_service_token
  - POST /v1/sandbox/lease-token/:id/attach-session: require_service_token
  - POST /v1/sandbox/usage/record             : require_service_token
  - GET  /v1/sandbox/usage/by-user/:userId    : require_self_or_admin
  - GET  /v1/sandbox/usage/by-tenant/:tenantId: require_service_token
  - GET  /v1/sandbox/usage/by-session/:sessionId: require_service_token
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

# app_id is a Postgres enum — only these values are accepted in activities.
# Internal sandbox adapters (rafael/private/business/unified-tester) are not
# enum members; their activity rows carry app_id = NULL.
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.tenant.middleware import (
    get_app_env_from_request as _get_app_env_from_request,
    normalize_app_env as _normalize_app_env,
)
from src.api_auth import (
    require_service_token,
    require_self_or_admin,
    AuthClaims,
)
from src.db.client import get_pool
from src.budget.gate import enforce_budget
from src.sandbox import account_router as _ar
from src.sandbox import lease_service as _ls
from src.sandbox.pricing import compute_hypothetical_cost_eur

logger = logging.getLogger(__name__)


async def _deduct_sandbox_budget(
    user_id: uuid.UUID,
    app: str,
    cost_eur: float,
) -> None:
    """
    Post-record budget deduction — best-effort, never raises.

    Analog to ai_call_writer._deduct_call_cost: the usage_events row is the
    authoritative spend record; this keeps user_budgets.usedEur in sync so
    the pre-lease gate has something to compare against. A failure here
    degrades to 'no deduction' — it must never break the caller's response.
    """
    try:
        from src.budget.plans import find_plan_for_app
        from src.budget.routes import apply_budget_deduction, BudgetDeductionDenied

        plan = find_plan_for_app(app)
        if plan is None:
            return  # app not in plan catalog — not budget-tracked
        try:
            await apply_budget_deduction(user_id, plan.id, cost_eur)
        except BudgetDeductionDenied as denied:
            logger.info(
                "sandbox post-record deduction denied (%s) user=%s app=%s",
                denied.reason, user_id, app,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("sandbox post-record budget deduction failed (non-blocking): %s", e)

router = APIRouter(prefix="/v1/sandbox", tags=["sandbox"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class LeaseTokenRequest(BaseModel):
    userId: uuid.UUID
    app: str
    estimatedDurationMin: int = Field(default=15, ge=1, le=480)
    preferredAccountId: Optional[str] = None


class LeaseTokenResponse(BaseModel):
    leaseId: uuid.UUID
    accountId: str
    oauthToken: str
    billingMode: str
    expiresAt: datetime
    tenantId: str


class HeartbeatResponse(BaseModel):
    ok: bool
    expiresAt: datetime


class ReleaseResponse(BaseModel):
    ok: bool
    alreadyReleased: bool


class AttachSessionRequest(BaseModel):
    sessionId: str


class AttachSessionResponse(BaseModel):
    ok: bool


class UsageRecordRequest(BaseModel):
    litellmCallId: str
    userId: uuid.UUID
    sessionId: str
    leaseId: Optional[uuid.UUID] = None
    accountId: str
    app: str
    model: str
    inputTokens: int
    outputTokens: int
    cacheReadTokens: int = 0
    cacheCreationTokens: int = 0
    hypotheticalCostEur: Optional[float] = None  # if omitted, Bridge computes


class UsageRecordResponse(BaseModel):
    ok: bool
    totalSessionTokens: int
    totalSessionHypotheticalEur: float


# ---------------------------------------------------------------------------
# POST /v1/sandbox/lease-token
# ---------------------------------------------------------------------------

@router.post("/lease-token", response_model=LeaseTokenResponse)
async def lease_token(
    body: LeaseTokenRequest,
    _claims: AuthClaims = Depends(require_service_token),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        # 1. Tenant + billing_mode (JIT-provisions the user if missing, so
        # first-lease for a new app-side identity does not fail with 404).
        info = await _ls.get_tenant_info(conn, body.userId, app=body.app)
        billing_mode = info["billing_mode"]
        tenant_id = info["tenant_id"]

        if billing_mode != "subscription":
            raise HTTPException(
                status_code=400,
                detail={"error": "lease_not_applicable", "billing_mode": billing_mode},
            )

        # 2. Pre-gate: block if user's budget is exhausted.
        # estimated_cost=0: we only need to detect "budget gone" states
        # (all_exhausted / monthly_exceeded_no_topup / trial_expired); the
        # gate lets unlicensed users through so trial users reach the sandbox.
        # Fail-open on DB/infra errors (enforce_budget swallows them).
        await enforce_budget(str(body.userId), body.app, 0.0)

        # 4. Pick best account — S7 fair round-robin: pass recent lease counts
        # so the router prefers under-used accounts over the highest-headroom
        # one. Window is 24h: long enough to smooth bursty per-user sessions,
        # short enough to forgive a once-busy account that has since gone idle.
        lease_counts = await _ls.get_recent_lease_counts(conn, window_hours=24)
        try:
            picked = await _ar.pick_account(body.preferredAccountId, lease_counts=lease_counts)
        except _ar.NoCapacityError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "no_capacity",
                    "retry_after_s": exc.retry_after_s,
                    "reasons": exc.reasons,
                },
            )
        except RuntimeError as exc:
            logger.error(f"account-pool-state failure: {exc}")
            raise HTTPException(status_code=503, detail={"error": "pool_state_unavailable", "detail": str(exc)})

        # 5. Read token file — fail loud on missing/empty
        try:
            oauth_token = _ls.read_oauth_token(picked.account_id)
        except RuntimeError as exc:
            logger.error(f"Token file read failed: {exc}")
            raise HTTPException(status_code=500, detail={"error": "token_file_error", "detail": str(exc)})

        # 6. Insert lease
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=body.estimatedDurationMin + 5)
        lease_id = await _ls.create_lease(
            conn,
            user_id=body.userId,
            tenant_id=tenant_id,
            app=body.app,
            account_id=picked.account_id,
            expires_at=expires_at,
        )

    logger.info(
        f"Lease issued: lease={lease_id} user={body.userId} account={picked.account_id} "
        f"app={body.app} expires={expires_at.isoformat()}"
    )

    return LeaseTokenResponse(
        leaseId=lease_id,
        accountId=picked.account_id,
        oauthToken=oauth_token,
        billingMode=billing_mode,
        expiresAt=expires_at,
        tenantId=tenant_id,
    )


# ---------------------------------------------------------------------------
# POST /v1/sandbox/lease-token/:leaseId/heartbeat
# ---------------------------------------------------------------------------

@router.post("/lease-token/{lease_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    lease_id: uuid.UUID,
    _claims: AuthClaims = Depends(require_service_token),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        new_expires_at = await _ls.heartbeat_lease(conn, lease_id)
    return HeartbeatResponse(ok=True, expiresAt=new_expires_at)


# ---------------------------------------------------------------------------
# POST /v1/sandbox/lease-token/:leaseId/release
# ---------------------------------------------------------------------------

@router.post("/lease-token/{lease_id}/release", response_model=ReleaseResponse)
async def release(
    lease_id: uuid.UUID,
    _claims: AuthClaims = Depends(require_service_token),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        just_released = await _ls.release_lease(conn, lease_id)
    return ReleaseResponse(ok=True, alreadyReleased=not just_released)


# ---------------------------------------------------------------------------
# POST /v1/sandbox/lease-token/:leaseId/attach-session
# ---------------------------------------------------------------------------

@router.post("/lease-token/{lease_id}/attach-session", response_model=AttachSessionResponse)
async def attach_session_endpoint(
    lease_id: uuid.UUID,
    body: AttachSessionRequest,
    _claims: AuthClaims = Depends(require_service_token),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        await _ls.attach_session(conn, lease_id, body.sessionId)
    return AttachSessionResponse(ok=True)


# ---------------------------------------------------------------------------
# POST /v1/sandbox/usage/record
# ---------------------------------------------------------------------------

@router.post("/usage/record", response_model=UsageRecordResponse)
async def record_usage(
    body: UsageRecordRequest,
    request: Request,
    _claims: AuthClaims = Depends(require_service_token),
):
    pool = get_pool()

    # Compute hypothetical cost if Daemon didn't send it
    hyp_cost = body.hypotheticalCostEur
    if hyp_cost is None:
        hyp_cost = compute_hypothetical_cost_eur(
            model=body.model,
            input_tokens=body.inputTokens,
            output_tokens=body.outputTokens,
            cache_read_tokens=body.cacheReadTokens,
            cache_creation_tokens=body.cacheCreationTokens,
        )

    # app_env: if daemon (or any caller) sets X-App-Env on this POST, the
    # sandbox call is attributable to a specific app variant. Most sandbox
    # calls don't — None is semantically correct then (`source='sandbox'`
    # already carries the dimensional distinction).
    app_env = _normalize_app_env(_get_app_env_from_request(request))

    async with pool.acquire() as conn:
        # Derive tenant_id + billing_mode
        info = await _ls.get_tenant_info(conn, body.userId)
        tenant_id = info["tenant_id"]
        billing_mode = info["billing_mode"]

        inserted = await _ls.record_usage(
            conn,
            litellm_call_id=body.litellmCallId,
            user_id=body.userId,
            tenant_id=tenant_id,
            session_id=body.sessionId,
            lease_id=body.leaseId,
            account_id=body.accountId,
            app=body.app,
            model=body.model,
            input_tokens=body.inputTokens,
            output_tokens=body.outputTokens,
            cache_read_tokens=body.cacheReadTokens,
            cache_creation_tokens=body.cacheCreationTokens,
            hypothetical_cost_eur=hyp_cost,
            billing_mode=billing_mode,
            app_env=app_env,
        )

        aggregate = await _ls.get_session_aggregate(conn, body.sessionId)

    # Deduct budget only when the row was actually inserted (not an idempotent
    # retry of the same litellm_call_id) to prevent double-counting.
    if inserted and hyp_cost > 0:
        await _deduct_sandbox_budget(body.userId, body.app, hyp_cost)

    return UsageRecordResponse(
        ok=True,
        totalSessionTokens=aggregate["total_session_tokens"],
        totalSessionHypotheticalEur=aggregate["total_session_hypothetical_eur"],
    )


# ---------------------------------------------------------------------------
# GET /v1/sandbox/usage/by-user/:userId
# ---------------------------------------------------------------------------

@router.get("/usage/by-user/{user_id}")
async def usage_by_user(
    user_id: uuid.UUID,
    since: Optional[datetime] = Query(default=None),
    claims: AuthClaims = Depends(require_self_or_admin),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        if since:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS event_count,
                    COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0) AS total_tokens,
                    COALESCE(SUM(hypothetical_cost_eur), 0) AS total_hypothetical_eur
                FROM usage_events
                WHERE source = 'sandbox' AND user_id = $1 AND recorded_at >= $2
                """,
                user_id,
                since,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS event_count,
                    COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0) AS total_tokens,
                    COALESCE(SUM(hypothetical_cost_eur), 0) AS total_hypothetical_eur
                FROM usage_events
                WHERE source = 'sandbox' AND user_id = $1
                """,
                user_id,
            )
    return {
        "userId": str(user_id),
        "since": since.isoformat() if since else None,
        "eventCount": int(row["event_count"]),
        "totalTokens": int(row["total_tokens"]),
        "totalHypotheticalEur": float(row["total_hypothetical_eur"]),
    }


# ---------------------------------------------------------------------------
# GET /v1/sandbox/usage/by-tenant/:tenantId
# ---------------------------------------------------------------------------

@router.get("/usage/by-tenant/{tenant_id}")
async def usage_by_tenant(
    tenant_id: str,
    since: Optional[datetime] = Query(default=None),
    _claims: AuthClaims = Depends(require_service_token),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        if since:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS event_count,
                    COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0) AS total_tokens,
                    COALESCE(SUM(hypothetical_cost_eur), 0) AS total_hypothetical_eur
                FROM usage_events
                WHERE source = 'sandbox' AND tenant_id = $1 AND recorded_at >= $2
                """,
                tenant_id,
                since,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS event_count,
                    COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0) AS total_tokens,
                    COALESCE(SUM(hypothetical_cost_eur), 0) AS total_hypothetical_eur
                FROM usage_events
                WHERE source = 'sandbox' AND tenant_id = $1
                """,
                tenant_id,
            )
    return {
        "tenantId": tenant_id,
        "since": since.isoformat() if since else None,
        "eventCount": int(row["event_count"]),
        "totalTokens": int(row["total_tokens"]),
        "totalHypotheticalEur": float(row["total_hypothetical_eur"]),
    }


# ---------------------------------------------------------------------------
# GET /v1/sandbox/usage/by-session/:sessionId
# ---------------------------------------------------------------------------

@router.get("/usage/by-session/{session_id}")
async def usage_by_session(
    session_id: str,
    _claims: AuthClaims = Depends(require_service_token),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        aggregate = await _ls.get_session_aggregate(conn, session_id)
        model_rows = await conn.fetch(
            """
            SELECT model, COUNT(*) AS calls,
                   SUM(input_tokens + output_tokens) AS tokens
            FROM usage_events
            WHERE source = 'sandbox' AND session_id = $1
            GROUP BY model
            """,
            session_id,
        )
    return {
        "sessionId": session_id,
        "totalSessionTokens": aggregate["total_session_tokens"],
        "totalSessionHypotheticalEur": aggregate["total_session_hypothetical_eur"],
        "byModel": [
            {"model": r["model"], "calls": int(r["calls"]), "tokens": int(r["tokens"])}
            for r in model_rows
        ],
    }
