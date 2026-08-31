"""platform-api endpoints for worker-internal reads/writes (ADR-0009 Schritt 2a).

Everything under /v1/internal/* exists so a worker can resolve or record
something it used to reach via a direct Postgres connection, via HTTP instead
(see src/platform_client.py — the client side of this contract). Every route
here is require_service_token-gated, exactly like the pre-existing
/v1/budget/check and /v1/budget/deduct.

Deliberately NOT exposed through the public nginx path (no /v1/internal
location block in docker/routes-platform-api.conf) — a worker on the same
Docker network, or a worker host reachable over Tailscale, talks to
platform-api directly. Nothing here is meant to be reachable from the public
load balancer at all.

Scope: principals (2a/C2), prepaid vision cap (2a/C6), the audit-event write
path (2a/C4), the budget-gate chain added in Schritt 2b (four read leaves plus
idempotent trial provisioning), and two further per-user read leaves: the
operator provider pin (users.provider_config) and the user's tenant
(users.tenant_id).

ERROR MAPPING IS LOAD-BEARING HERE. The budget chain has two deliberate
fail-loud safeguards (AmbiguousProjectBudget, LegacyTopUpBalanceError). Both
MUST leave this module as a 4xx, never a 5xx: platform_client turns any 5xx
into PlatformUnavailable, and the worker's gate wraps the whole budget call in
`except Exception -> letting call through`. A 5xx would therefore convert a
"stop, this would mis-bill" alarm into a silent pass-through — the exact
opposite of what those exceptions exist for. 409 is used for both: the query
ran and gave a definitive, non-retryable answer that the caller must handle.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from src.api_auth import require_service_token, AuthClaims
from src.audit.db_writer import insert_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/internal", tags=["internal"])


# ── C2: principals ───────────────────────────────────────────────────────

@router.get("/principals/{token_hash}")
async def get_principal_by_hash(
    token_hash: str,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.principals.get_principal_row_by_hash — the same query a
    worker's own direct-DB fallback runs. 404 is a real, cacheable answer
    ("no active principal with that hash"), not an error."""
    from src.principals import get_principal_row_by_hash

    row = await get_principal_row_by_hash(token_hash)
    # Wrapped + 200/null instead of a bare row + 404, for the same reason as
    # lookup-by-email above: an undeployed route also answers 404, and the
    # caller must be able to tell "no such principal" (authoritative, cacheable)
    # from "platform-api does not know this route" (fall back to the DB).
    return {"principal": row}


# ── C6: prepaid vision cap ───────────────────────────────────────────────

@router.get("/prepaid-vision/spent-24h")
async def get_prepaid_vision_spent_24h(
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.routing.prepaid_cap's direct query — rolling-24h prepaid
    vision spend in EUR. No 404 case: absence of spend is 0.0, not "not found"."""
    from src.routing.prepaid_cap import query_spent_last_24h_from_db

    spent = await query_spent_last_24h_from_db()
    return {"spent_eur": spent}


# ── Schritt 2d: research-cloud daily cap (same shape as prepaid vision) ──

@router.get("/research-cloud/spent-24h")
async def get_research_cloud_spent_24h(
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.research_cloud.cap's direct query — rolling-24h research-cloud
    real spend in EUR. No 404 case: absence of spend is 0.0, not "not found"."""
    from src.research_cloud.cap import query_spent_last_24h_from_db

    spent = await query_spent_last_24h_from_db()
    return {"spent_eur": spent}


# ── Schritt 2d: anonymization accountability counters ────────────────────

@router.get("/audit/anonymization-metrics")
async def get_anonymization_metrics_internal(
    hours: int = 24,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors the worker's public GET /v1/metrics/anonymization (minus the
    "db" envelope key, which describes the WORKER's resolution stage). A DB
    failure propagates as 5xx — never an all-zeros body, see
    src.audit.anonymization_metrics."""
    from src.audit.anonymization_metrics import query_anonymization_metrics_from_db

    return await query_anonymization_metrics_from_db(hours)


# ── C4: audit-event write path ───────────────────────────────────────────

class AuditEventRequest(BaseModel):
    action: str = Field(..., min_length=1)
    actor_user_id: Optional[str] = None
    actor_label: Optional[str] = None
    target_kind: Optional[str] = None
    target_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


@router.post("/audit-events", status_code=204)
async def post_audit_event(
    body: AuditEventRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Response:
    """Mirrors POST /v1/audit/log's INSERT, but takes an explicit
    actor_user_id in the body — the JWT-derived actor of /v1/audit/log doesn't
    apply to a service-token worker call recording an action on someone else's
    behalf (e.g. the pseudonymization attestation, see src/audit/recorder.py).
    """
    await insert_audit_event(
        action=body.action,
        actor_user_id=body.actor_user_id,
        actor_label=body.actor_label,
        target_kind=body.target_kind,
        target_id=body.target_id,
        metadata=body.metadata,
    )
    return Response(status_code=204)


# ── 2b/C1: identity — email → user id ────────────────────────────────────

class EmailLookupRequest(BaseModel):
    email: str = Field(..., min_length=3)


@router.post("/users/lookup-by-email")
async def post_lookup_user_by_email(
    body: EmailLookupRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.identity.user_resolver.lookup_user_id_by_email.

    POST, not GET, on purpose: the email is the lookup key and would otherwise
    sit in URLs, access logs and traces. A body keeps it out of all three. 404
    is a real answer ("no Bridge user for this identity"), which the caller
    turns back into UnknownUserIdentity.
    """
    from src.identity.user_resolver import lookup_user_id_by_email

    uid = await lookup_user_id_by_email(body.email)
    # 200 with id=null, NOT 404, for "no such user". 404 is reserved for "this
    # route does not exist" — which is exactly what a platform-api that has not
    # been deployed yet answers (measured on dev-bridge, 2026-08-20). Overloading
    # 404 would make a missing deployment indistinguishable from a missing user,
    # and since 404 is an ordinary response it would NOT trigger the caller's
    # fallback: every Engelmann identity would silently become "unknown".
    return {"id": str(uid) if uid is not None else None}


# ── worker-DB-free reads: per-user provider pin ──────────────────────────

@router.get("/users/{user_id}/provider-config")
async def get_user_provider_config_route(
    user_id: str,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.routing.user_provider_override.fetch_provider_config_from_db
    — the operator-set backend pin (users.provider_config).

    200 with providerConfig=null for "no such user / no pin", NOT 404: an
    undeployed platform-api also answers 404, and the caller must be able to
    tell "this user is not pinned" (authoritative) from "this route does not
    exist" (fall back to the DB). Getting that wrong here is worse than on the
    other read leaves — the caller is fail-CLOSED precisely because reading a
    pin as absent would silently move a customer's traffic to another
    jurisdiction.

    A DB failure stays a 5xx on purpose. platform_client turns that into
    PlatformUnavailable, which is exactly what the caller must hear: it then
    falls back to its own direct connection or refuses the call. Flattening it
    into providerConfig=null would be the silent mis-route.
    """
    import uuid as _uuid

    from src.routing.user_provider_override import fetch_provider_config_from_db

    try:
        uid = _uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid user id: {user_id!r}")

    return {"providerConfig": await fetch_provider_config_from_db(uid)}


# ── worker-DB-free reads: user → tenant ──────────────────────────────────

@router.get("/users/{user_id}/tenant")
async def get_user_tenant_route(
    user_id: str,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.api_auth.tenant_resolver.fetch_user_tenant_row.

    Returns {found, tenantId} rather than a bare tenant id, because the caller
    genuinely branches on the difference: resolve_tenant_for_user answers
    "Unknown user" for found=false and "user has no tenant_id" for
    found=true/tenantId=null, and those are two different 400s an operator
    reads differently. Collapsing both into null would erase that.

    200 in every one of those cases, not 404 — same reason as the pin route
    above: 404 is reserved for "this route is not deployed".
    """
    import uuid as _uuid

    from src.api_auth.tenant_resolver import fetch_user_tenant_row

    try:
        uid = _uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid user id: {user_id!r}")

    row = await fetch_user_tenant_row(uid)
    return {"found": row is not None, "tenantId": row["tenantId"] if row else None}


# ── 2b/C2: which plan holds an allocated budget for this project? ────────

@router.get("/project-budgets/allocated-plan-id")
async def get_allocated_plan_id(
    user_id: str,
    project_id: str,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.billing.project_budgets_service.find_allocated_plan_id.

    Returns 200 with planId=null when nothing is allocated — unlike principals'
    404, because "no allocation" is the NORMAL case here, not an exception:
    every ordinary report call carries a project_id and legitimately draws from
    the monthly pot. A 404 would invite a caller to treat the common path as an
    error.

    AmbiguousProjectBudget -> 409, never 5xx (see module docstring): several
    plans allocated the same project, which must fail loud rather than pick one
    and mis-bill.
    """
    import uuid as _uuid

    from src.billing.project_budgets_service import (
        AmbiguousProjectBudget,
        find_allocated_plan_id,
    )

    try:
        plan_id = await find_allocated_plan_id(_uuid.UUID(user_id), project_id)
    except AmbiguousProjectBudget as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "ambiguous_project_budget", "message": str(e)},
        ) from e
    return {"planId": plan_id}


# ── 2b/C3: project-pot state ─────────────────────────────────────────────

class ProjectBudgetStateRequest(BaseModel):
    user_id: str
    plan_id: str
    project_id: str
    estimated_cost_eur: float = 0.0


@router.post("/project-budgets/state")
async def post_project_budget_state(
    body: ProjectBudgetStateRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.billing.project_budgets_service.evaluate — returns
    {exists, allowed, remainingEur, topUpRemainingEur}.

    This one leaf legitimately returns a verdict rather than raw rows, because
    `evaluate` is already the project path's own decision function today; the
    worker never had separate branching on top of it. That is NOT true of the
    monthly path — see post_user_budget_state.
    """
    import uuid as _uuid

    from src.billing.project_budgets_service import evaluate

    return await evaluate(
        _uuid.UUID(body.user_id), body.plan_id, body.project_id, body.estimated_cost_eur
    )


# ── 2b/C4: monthly-pot state (read half) ─────────────────────────────────

class UserBudgetStateRequest(BaseModel):
    user_id: str


@router.post("/budget/user-budget-state")
async def post_user_budget_state(
    body: UserBudgetStateRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.budget.routes.load_user_budget_state — DATA, not a verdict.

    The gate's own computation (rollover, check_budget, trial expiry) stays in
    the worker: those are stateless pure functions, and duplicating their
    branching here is precisely what Schritt 2b's design rejected.

    LegacyTopUpBalanceError -> 409, never 5xx (see module docstring).
    """
    import uuid as _uuid

    from src.budget.routes import load_user_budget_state
    from src.budget.topup_store import LegacyTopUpBalanceError

    try:
        return await load_user_budget_state(_uuid.UUID(body.user_id))
    except LegacyTopUpBalanceError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "legacy_topup_balance",
                "message": str(e),
                "userId": str(e.user_id),
                "balanceEur": e.balance_eur,
            },
        ) from e


# ── 2b/D2: idempotent trial provisioning (the one write) ─────────────────

class EnsureTrialRequest(BaseModel):
    user_id: str
    plan_id: str


@router.post("/budget/ensure-trial")
async def post_ensure_trial(
    body: EnsureTrialRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.budget.routes.ensure_trial_provisioned.

    The only write in the whole budget-gate chain. Idempotent by construction
    (INSERT … ON CONFLICT … WHERE the trial key IS NULL), which is what makes
    it safe for the caller to opt into platform_client's bounded retry — a
    replay after a lost answer cannot double-provision or reset a running
    trial window.

    Returns the refreshed state alongside the outcome so the caller needs one
    round trip, exactly as the in-process path does today (provision, reload).
    """
    import uuid as _uuid

    from src.budget.routes import ensure_trial_provisioned
    from src.budget.topup_store import LegacyTopUpBalanceError

    try:
        return await ensure_trial_provisioned(_uuid.UUID(body.user_id), body.plan_id)
    except LegacyTopUpBalanceError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "legacy_topup_balance",
                "message": str(e),
                "userId": str(e.user_id),
                "balanceEur": e.balance_eur,
            },
        ) from e


# ── 2c/C1: anonymous identity probe ──────────────────────────────────────

@router.get("/identity/anonymous")
async def get_anonymous_identity(
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.activity.ledger_db.anonymous_identity_present.

    200 {"present": false} is a real answer ("migration 032 has not run"), not
    an error — the caller holds the call in its write-ahead spool until it
    becomes true, rather than dropping an anonymous booking. A DB failure here
    must NOT be flattened into present=false: it becomes a 5xx, the caller
    hears PlatformUnavailable, and the row likewise stays owed.
    """
    from src.activity.ledger_db import anonymous_identity_present

    return {"present": await anonymous_identity_present()}


# ── 2c/C2: billing context (users ⋈ tenants) ─────────────────────────────

@router.get("/users/{user_id}/billing-context")
async def get_billing_context(
    user_id: str,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.activity.ledger_db.load_billing_context — DATA, not a verdict.

    The billing_mode → (billing_mode enum, real_cost_eur) mapping stays in the
    worker (`resolve_ledger_cost`, pure and unit-tested); this leaf only reports
    what the two tables say.

    200 with context=null for "no such user / dangling tenant", NOT 404 — same
    reasoning as users/lookup-by-email: an undeployed platform-api also answers
    404, and overloading it would make a missing deployment indistinguishable
    from a missing tenant. Here that would be worse than on a read path: the
    caller would file a real, billable call as the definitive skip
    "skipped:no_tenant" and release it from the spool.
    """
    import uuid as _uuid

    from src.activity.ledger_db import load_billing_context

    try:
        _uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid user id: {user_id!r}")

    return {"context": await load_billing_context(user_id)}


# ── 2c/C3: the authoritative billing row (+ its audit row) ───────────────

class AiCallLedgerRequest(BaseModel):
    # Idempotency is the whole contract of this endpoint: the key is generated
    # where the CALL happened, so a replay an hour later is recognisably the
    # same call and returns outcome="duplicate" instead of a second row.
    idempotency_key: str = Field(..., min_length=1)
    # ISO-8601, the call's ORIGIN time — never the arrival time here.
    recorded_at: str = Field(..., min_length=1)
    actor_user_id: Optional[str] = None
    tenant_id: str = Field(..., min_length=1)
    app: Optional[str] = None
    app_env: Optional[str] = None
    model: str
    provider: str
    region: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    billing_mode: str
    real_cost_eur: float
    hypothetical_cost_eur: float
    pricing_version: str
    provider_metadata: Dict[str, Any] = Field(default_factory=dict)
    audit_event_type: str
    audit_payload: Dict[str, Any] = Field(default_factory=dict)


@router.post("/usage/ai-call")
async def post_ai_call_ledger(
    body: AiCallLedgerRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """The one write the worker's money path cannot do without: one usage_events
    row plus, only if that row was created here, its `activities` audit row.

    Mirrors src.activity.ledger_db.insert_ai_call. Returns
    {"outcome": "written"|"duplicate", "auditWritten": bool}.

    Why this endpoint exists instead of reusing POST /v1/activity/log: that
    route writes `activities` ONLY — it has no usage_events columns at all
    (tokens, provider, billing_mode, real_cost, idempotency_key), stamps
    timestamp = NOW() rather than the call's origin time, and 400s on an app id
    outside its allowlist where this path deliberately books NULL + app_id_raw.
    It is the counterpart of the audit half, not of the money row.

    ERROR MAPPING IS LOAD-BEARING (see module docstring), with the opposite
    polarity to the budget chain: a failure here MUST reach the caller as a
    non-2xx. The caller then treats the call as still owed and its write-ahead
    spool replays it. A failure dressed up as a 200 would ack the spool record
    and turn a retryable miss into unbilled usage — the exact loss Schritt 1
    was built to close.
    """
    from datetime import datetime as _dt, timezone as _tz

    from src.activity.ledger_db import insert_ai_call

    try:
        recorded_at = _dt.fromisoformat(body.recorded_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid recorded_at: {body.recorded_at!r} (expected ISO-8601)",
        )
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=_tz.utc)

    try:
        result = await insert_ai_call(
            idempotency_key=body.idempotency_key,
            recorded_at=recorded_at,
            actor_user_id=body.actor_user_id,
            tenant_id=body.tenant_id,
            app=body.app,
            app_env=body.app_env,
            model=body.model,
            provider=body.provider,
            region=body.region,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
            cache_read_tokens=body.cache_read_tokens,
            cache_creation_tokens=body.cache_creation_tokens,
            billing_mode=body.billing_mode,
            real_cost_eur=body.real_cost_eur,
            hypothetical_cost_eur=body.hypothetical_cost_eur,
            pricing_version=body.pricing_version,
            provider_metadata=body.provider_metadata,
            audit_event_type=body.audit_event_type,
            audit_payload=body.audit_payload,
        )
    except Exception as e:  # noqa: BLE001 — must surface as 5xx, never as a 200
        logger.error(
            "internal/usage/ai-call: LEDGER WRITE FAILED (call=%s app=%s model=%s "
            "provider=%s): %s — answering 503 so the worker keeps the row in its "
            "write-ahead spool",
            body.idempotency_key, body.app, body.model, body.provider, e,
        )
        raise HTTPException(status_code=503, detail=f"ledger write failed: {e}") from e

    return {
        "outcome": "written" if result.created else "duplicate",
        "auditWritten": result.audit_written,
    }


# ── 2c/C4: post-call deduction, project-plan half ────────────────────────

class ProjectBudgetDeductRequest(BaseModel):
    user_id: str
    plan_id: str
    project_id: str
    cost_eur: float
    allocate_limit_eur: Optional[float] = None
    tenant_id: Optional[str] = None


@router.post("/project-budgets/deduct")
async def post_project_budget_deduct(
    body: ProjectBudgetDeductRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.billing.project_budgets_service.deduct — the project-plan
    counterpart of the pre-existing POST /v1/budget/deduct, which covers only
    monthly plans (`_require_month_interval`).

    NOT IDEMPOTENT, like its monthly sibling: a read-modify-write on
    project_budgets plus a FIFO draw through the TopUp lots, with no dedup key.
    The caller must never retry it and never replay it — the deduction is bound
    to "the ledger INSERT created the row in THIS attempt", which happens at
    most once per call.
    """
    import uuid as _uuid

    from src.billing.project_budgets_service import deduct

    try:
        uid = _uuid.UUID(body.user_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid user id: {body.user_id!r}")

    return await deduct(
        uid,
        body.plan_id,
        body.project_id,
        body.cost_eur,
        allocate_limit_eur=body.allocate_limit_eur,
        tenant_id=body.tenant_id,
    )


# ── 2c/C5: app-tier policy (who pays for this call-site) ─────────────────

@router.get("/app-tier-policy")
async def get_app_tier_policy(
    app_id: str,
    agent_id: Optional[str] = None,
    app_env: Optional[str] = None,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.routing.app_tier_policy.lookup_policy_from_db.

    200 {"policy": null} means "no matching row" — the normal case, and a real
    answer. A DB failure must NOT be flattened into that: it propagates and
    becomes a 5xx, so the caller can tell "no policy" from "could not ask" and
    log the latter. The caller still fails open either way (this is a cost
    optimisation, never a reason to 503 a live call), but only one of the two
    is worth a warning — see _lookup_policy.
    """
    from src.routing.app_tier_policy import lookup_policy_from_db

    policy = await lookup_policy_from_db(app_id, agent_id, app_env)
    return {
        "policy": None if policy is None else {
            "target_tier": policy.target_tier,
            "billing_account": policy.billing_account,
        }
    }


# ── 2c/C6: the app_id enum (so a DB-free worker can still validate) ──────

@router.get("/app-id-enum")
async def get_app_id_enum(
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.activity.app_registry.read_app_id_enum_from_db.

    Read once at worker startup. A worker without its own database still writes
    ledger rows (through POST /v1/internal/usage/ai-call), so it still has to be
    able to tell a real app from a call-site label like "bridge-jobs" BEFORE the
    value reaches an ENUM column — see load_known_app_ids for why validation
    going dark there is not the harmless no-op it used to be.

    A failure propagates as a 5xx and the worker refuses to boot. That is the
    intended severity: booting without the list books the whole fleet as
    app=NULL.
    """
    from src.activity.app_registry import read_app_id_enum_from_db

    return {"members": sorted(await read_app_id_enum_from_db())}


# ── Schritt 2d: the durable job store (ADR-0009 Weg b, /v1/jobs) ─────────
#
# Mirrors src.jobs.store 1:1 — platform-api is the process that holds the
# pool, so the SQL stays in store.py and these routes are thin, explicit
# wrappers. The worker side is src.jobs.store_client (same function names,
# platform-first with direct-DB fallback while workers still carry
# BRIDGE_DB_URL).
#
# Scope note ("Worker duerfen nicht mehr koennen als vorher"): with a direct
# BRIDGE_DB_URL a worker could run arbitrary SQL against the customer
# database. Through these routes it can exactly create/read/advance jobs —
# a strict reduction. Everything stays require_service_token-gated and off
# the public nginx path like the rest of this module.
#
# claim-stale is the one operation that MUST live here: its FOR UPDATE SKIP
# LOCKED atomicity only exists inside a single SQL statement. Over HTTP it
# stays exactly as atomic — the statement runs here, in one piece.


class InternalJobCreate(BaseModel):
    job_id: str
    kind: str
    payload: Optional[Dict[str, Any]] = None
    attribution: Optional[Dict[str, Any]] = None


class InternalJobProgress(BaseModel):
    progress: Dict[str, Any]


class InternalJobDone(BaseModel):
    result: Optional[Dict[str, Any]] = None


class InternalJobError(BaseModel):
    message: str
    code: Optional[str] = None


class InternalJobDefer(BaseModel):
    delay_seconds: int = Field(ge=1, le=24 * 3600)
    reason: str


class InternalJobClaimStale(BaseModel):
    stale_seconds: int = Field(ge=1)
    max_attempts: int = Field(ge=1)


class InternalJobCleanup(BaseModel):
    ttl_seconds: int = Field(ge=60)


@router.post("/jobs", status_code=204)
async def internal_create_job(
    body: InternalJobCreate,
    _claims: AuthClaims = Depends(require_service_token),
) -> Response:
    """Insert a fresh 'pending' job. Idempotent on job_id (ON CONFLICT DO
    NOTHING) — a client retry after a lost answer cannot create a second row."""
    from src.jobs import store

    await store.create_job(body.job_id, body.kind, body.payload, body.attribution)
    return Response(status_code=204)


@router.get("/jobs")
async def internal_list_jobs(
    app_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """List projection, scoped. ValueError from the store (missing scope, bad
    status/limit) is a caller mistake → 400, never a 5xx (a 5xx would read as
    "platform-api down" to platform_client and trigger the worker's fallback)."""
    from src.jobs import store

    try:
        jobs = await store.list_jobs(app_id=app_id, user_id=user_id, status=status, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"jobs": jobs}


@router.get("/jobs/{job_id}")
async def internal_get_job(
    job_id: str,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Full job row. 404 = unknown id — a real, interpretable answer (the
    public GET /v1/jobs/{id} turns it into its own 404), not an outage."""
    from src.jobs import store

    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return {"job": job}


@router.post("/jobs/{job_id}/mark-running", status_code=204)
async def internal_mark_running(
    job_id: str,
    _claims: AuthClaims = Depends(require_service_token),
) -> Response:
    from src.jobs import store

    await store.mark_running(job_id)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/heartbeat", status_code=204)
async def internal_heartbeat(
    job_id: str,
    _claims: AuthClaims = Depends(require_service_token),
) -> Response:
    from src.jobs import store

    await store.heartbeat(job_id)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/progress", status_code=204)
async def internal_update_progress(
    job_id: str,
    body: InternalJobProgress,
    _claims: AuthClaims = Depends(require_service_token),
) -> Response:
    from src.jobs import store

    await store.update_progress(job_id, body.progress)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/done", status_code=204)
async def internal_mark_done(
    job_id: str,
    body: InternalJobDone,
    _claims: AuthClaims = Depends(require_service_token),
) -> Response:
    from src.jobs import store

    await store.mark_done(job_id, body.result)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/error", status_code=204)
async def internal_mark_error(
    job_id: str,
    body: InternalJobError,
    _claims: AuthClaims = Depends(require_service_token),
) -> Response:
    from src.jobs import store

    await store.mark_error(job_id, body.message, code=body.code)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/defer", status_code=204)
async def internal_defer_job(
    job_id: str,
    body: InternalJobDefer,
    _claims: AuthClaims = Depends(require_service_token),
) -> Response:
    from src.jobs import store

    await store.defer_job(job_id, body.delay_seconds, body.reason)
    return Response(status_code=204)


@router.post("/jobs-maintenance/claim-stale")
async def internal_claim_stale_job(
    body: InternalJobClaimStale,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Atomically claim ONE stale-but-retryable job (FOR UPDATE SKIP LOCKED —
    concurrent callers each get a DIFFERENT row or none). {"job": null} means
    "nothing claimable", a normal answer, not an error."""
    from src.jobs import store

    job = await store.claim_stale_job(body.stale_seconds, body.max_attempts)
    return {"job": job}


@router.get("/jobs-maintenance/abandoned")
async def internal_find_abandoned(
    stale_seconds: int,
    max_attempts: int,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    from src.jobs import store

    jobs = await store.find_abandoned(stale_seconds, max_attempts)
    return {"jobs": jobs}


@router.post("/jobs-maintenance/cleanup")
async def internal_cleanup_old(
    body: InternalJobCleanup,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    from src.jobs import store

    removed = await store.cleanup_old(body.ttl_seconds)
    return {"removed": removed}
