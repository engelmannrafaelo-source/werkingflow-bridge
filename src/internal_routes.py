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
path (2a/C4), and — added in Schritt 2b — the budget-gate chain: four read
leaves plus idempotent trial provisioning.

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
