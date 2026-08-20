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
    if row is None:
        raise HTTPException(status_code=404, detail="no active principal with that token hash")
    return row


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
    if uid is None:
        # No PII in the message — same rule as the worker-side path.
        raise HTTPException(status_code=404, detail="no Bridge user for the given email identity")
    return {"id": str(uid)}


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
