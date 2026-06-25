"""
Billing-Endpoints — Cross-App Mollie-Integration.

Auth model:
  - /v1/billing/mollie-webhook            PUBLIC (Mollie calls us; we verify by re-fetching payment from Mollie)
  - /v1/billing/customer                  require_jwt_or_service
  - /v1/billing/subscription/...          require_jwt_or_service
  - /v1/billing/topup/...                 require_jwt_or_service
  - /v1/billing/{user_id}/...             require_self_or_admin
  - /v1/billing/order/invoice             require_jwt_or_service (Rechnungs-Lane)
  - /v1/users/{user_id}/pending-orders    require_self_or_admin
  - /v1/admin/orders/pending              require_admin (operator only)
  - /v1/admin/orders/{order_id}/release   require_admin (operator only)
"""
import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response
from pydantic import BaseModel, Field

from src.billing import billing_service
from src.api_auth import require_admin, require_jwt_or_service, require_self_or_admin, require_service_token, AuthClaims
from src.budget.plans import PLANS
from src.db.client import get_pool

router = APIRouter(prefix="/v1/billing", tags=["billing"])

_ALLOWED_APP_IDS = {
    "werking-report", "werking-energy", "werking-safety",
    "werking-noise", "engelmann",
}

_ALLOWED_ACCOUNT_TYPES = {"customer", "test", "internal"}


# ---------------------------------------------------------------------------
# Overview — admin dashboard cross-tenant aggregates
# ---------------------------------------------------------------------------

@router.get("/overview")
async def billing_overview(
    app: Optional[str] = Query(
        default=None,
        description=(
            "Filter subscription stats to one app (werking-report|werking-energy|"
            "werking-safety|werking-noise|engelmann). Top-up totals are "
            "app-overarching and are returned as null when filtered."
        ),
    ),
    account_type: Optional[str] = Query(
        default=None,
        description="Filter by tenant account type: customer|test|internal",
    ),
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Cross-tenant billing snapshot for the Platform Admin dashboard.

    Reads two sources:
      1. `subscriptions` table — authoritative once Mollie write-through is on
      2. `activities` table — fallback during migration (rows imported from
         the old werking-report storage live here as
         category='billing' / eventType='subscription.imported.<status>')

    MRR is estimated, not invoiced: trial=€0, plan-prices from
    src/budget/plans.py multiplied by seats. Replaces with real Mollie totals
    once the live billing path is hot.

    Optional `app` filter restricts subscription-side stats to one app.
    Top-up revenue is intentionally NOT app-attributable (credit_purchases
    has no app dimension — top-ups are a wallet, used across apps) and is
    returned as null when filtered, so the dashboard doesn't surface a
    misleading "energy made €X in top-ups".
    """
    if app and app not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown app: {app}")
    if account_type and account_type not in _ALLOWED_ACCOUNT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid account_type: {account_type!r}. Must be customer|test|internal",
        )

    pool = get_pool()
    plan_prices = {pid: p.price for pid, p in PLANS.items()}
    # Legacy plan-id aliases from werking-report's own catalog. Remove once the
    # migration script in apps/werking-report/scripts/ rewrites plan_ids on insert.
    plan_prices["standard"] = plan_prices.get("report-standard", 250)
    plan_prices["pro"] = plan_prices.get("report-standard", 250)

    by_plan: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    by_app: Dict[str, int] = {}
    total_mrr_eur = 0.0
    active_subs = 0
    cancelled_subs = 0
    source = "subscriptions"

    async with pool.acquire() as conn:
        conds: List[str] = []
        params: List[Any] = []

        def _add_cond(cond: str, val: Any) -> None:
            params.append(val)
            conds.append(cond.replace("$$", f"${len(params)}"))

        if app:
            _add_cond("s.app_id = $$::app_id", app)
        if account_type:
            _add_cond("t.account_type = $$::account_type", account_type)

        where_clause = f"WHERE {' AND '.join(conds)}" if conds else ""
        if account_type:
            sub_rows = await conn.fetch(
                f"""
                SELECT s.status, s.plan_id, s.seats, s.app_id
                FROM subscriptions s
                JOIN users u ON u.id = s.user_id
                JOIN tenants t ON t.id = u.tenant_id
                {where_clause}
                """,
                *params,
            )
        elif app:
            sub_rows = await conn.fetch(
                "SELECT status, plan_id, seats, app_id FROM subscriptions WHERE app_id = $1::app_id",
                app,
            )
        else:
            sub_rows = await conn.fetch(
                "SELECT status, plan_id, seats, app_id FROM subscriptions"
            )

    if sub_rows:
        for r in sub_rows:
            status = r["status"]
            plan_id = r["plan_id"]
            seats = r["seats"] or 1
            app_id = r["app_id"]

            by_status[status] = by_status.get(status, 0) + 1
            by_plan[plan_id] = by_plan.get(plan_id, 0) + 1
            by_app[app_id] = by_app.get(app_id, 0) + 1

            if status == "active":
                active_subs += 1
                total_mrr_eur += float(plan_prices.get(plan_id, 0)) * seats
            elif status == "cancelled":
                cancelled_subs += 1
    else:
        # Subscriptions table empty — derive from migrated activities so
        # the dashboard isn't blank during the WR→Bridge transition.
        source = "activities"
        async with pool.acquire() as conn:
            if app:
                rows = await conn.fetch(
                    """
                    SELECT payload, app_id
                    FROM activities
                    WHERE category = 'billing'
                      AND event_type LIKE 'subscription.imported.%'
                      AND app_id = $1::app_id
                    """,
                    app,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT payload, app_id
                    FROM activities
                    WHERE category = 'billing'
                      AND event_type LIKE 'subscription.imported.%'
                    """
                )
        for r in rows:
            payload = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
            plan_id = payload.get("planId", "unknown")
            status = payload.get("status", "unknown")
            seats = int(payload.get("seats", 1) or 1)
            app_id = r["app_id"] or "werking-report"

            by_status[status] = by_status.get(status, 0) + 1
            by_plan[plan_id] = by_plan.get(plan_id, 0) + 1
            by_app[app_id] = by_app.get(app_id, 0) + 1

            if status == "active":
                active_subs += 1
                total_mrr_eur += float(plan_prices.get(plan_id, 0)) * seats
            elif status == "cancelled":
                cancelled_subs += 1

    # Top-up totals — app-overarching. Omit when filtering by app so the
    # dashboard doesn't misattribute cross-app wallet activity to one app.
    if app:
        topup_revenue: Optional[float] = None
        topup_count: Optional[int] = None
        topup_balance: Optional[float] = None
    else:
        async with pool.acquire() as conn:
            topup_sum_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(pack_eur), 0) AS total, COUNT(*) AS count FROM credit_purchases"
            )
            topup_balances_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(balance_eur), 0) AS total FROM user_topup_balances"
            )
        topup_revenue = float(topup_sum_row["total"])
        topup_count = int(topup_sum_row["count"])
        topup_balance = float(topup_balances_row["total"])

    return {
        "source": source,
        "app": app,
        "totalMrrEur": round(total_mrr_eur, 2),
        "activeSubscriptions": active_subs,
        "cancelledSubscriptions": cancelled_subs,
        "totalTopupRevenueEur": topup_revenue,
        "topupPurchasesCount": topup_count,
        "totalUserBalanceEur": topup_balance,
        "byPlan": by_plan,
        "byStatus": by_status,
        "byApp": by_app,
    }


@router.get("/plans")
async def list_plans() -> Dict[str, Any]:
    """
    Public plan catalog — pricing info for the customer portal.

    No auth required: prices are not sensitive; the frontend needs this
    to render the plan-comparison table before (and after) the user logs in.

    Source: PLANS dict in src/budget/plans.py, populated from the `plans`
    table by reload_plans() at Bridge startup and on /plans/reload.
    """
    return {
        "plans": [
            {
                "id": p.id,
                "appId": p.app_id,
                "name": p.name,
                "priceEur": p.price,
                "interval": p.interval,
                "apiBudgetEur": p.api_budget_eur,
                "description": p.description,
                "trial": p.trial,
            }
            for p in PLANS.values()
        ]
    }


@router.post("/plans/reload")
async def reload_plans_endpoint(
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Re-read the plans table into the in-memory PLANS cache.

    Operator-only (require_admin) — gated on is_operator, so only service
    tokens without X-User-ID or admin JWTs can hot-swap pricing. A scoped
    customer-proxy token must never reach this endpoint.

    Use case: after a UPDATE plans SET price_eur = ... statement, call this
    to make the new price visible without restarting the Bridge. The
    /plans GET endpoint reflects the new state on the very next call.

    Returns the new active-plan count.
    """
    from src.budget.plans import reload_plans
    count = await reload_plans()
    return {"reloaded": True, "activePlans": count}


class CustomerRequest(BaseModel):
    userId: str
    email: str
    name: str


@router.post("/customer")
async def billing_customer(
    body: CustomerRequest,
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    return await billing_service.get_or_create_customer(body.userId, body.email, body.name)


async def _resolve_billing_identity(
    claims: AuthClaims,
    body_user_id: Optional[str],
    body_email: Optional[str],
    body_name: Optional[str],
) -> tuple[str, str, str]:
    """
    Resolve (user_id, email, name) for a Mollie checkout call.

    Source-of-truth: the auth context's acting_user_id wins over body.userId
    (clients cannot spoof identity). Operator service-token calls (no
    acting_user_id) fall back to body.userId. email/name prefer body when
    explicitly provided, otherwise look them up from the users table.

    Fails fast at every step:
      - 400 if userId can't be resolved at all
      - 400 if userId is malformed
      - 404 if userId doesn't exist in users
      - 500 if users row has missing email/name (data-integrity bug — never
        paper over silently, would produce malformed Mollie customers)
    """
    user_id = claims.effective_user_id or body_user_id
    if not user_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "userId could not be resolved: pass it in the body (operator "
                "service token) or via X-User-ID header / Bearer JWT."
            ),
        )

    email = body_email
    name = body_name
    if not email or not name:
        try:
            user_uuid = uuid.UUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid userId: {user_id!r}")
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT email, name FROM users WHERE id = $1", user_uuid,
            )
        if not row:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
        email = email or row["email"]
        name = name or row["name"]
        if not email or not name:
            raise HTTPException(
                status_code=500,
                detail=f"User '{user_id}' has missing email or name in users table",
            )

    return user_id, email, name


class SubscriptionCheckoutRequest(BaseModel):
    # planId + successRedirect are always required: only the client knows where
    # Mollie should redirect after payment, and which plan to purchase.
    planId: str
    successRedirect: str
    # seats must be a positive small integer. Above 100 we suspect API abuse —
    # a single buyer would never legitimately seat 100 users at once via this
    # endpoint. Tighten further once the real cap is known.
    seats: int = Field(default=1, ge=1, le=100)
    # userId/email/name are derived from the auth context (acting_user_id +
    # users-table lookup) — see _resolve_billing_identity. They remain accepted
    # in the body for operator-mode service-token calls (no X-User-ID header)
    # where Bridge has no auth user context to derive them from. Defence in
    # depth: when the auth context provides acting_user_id, body.userId is
    # ignored (clients cannot spoof identity by sending a different userId).
    userId: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None


class SubscriptionProvisionRequest(BaseModel):
    userId: str
    planId: str
    seats: int = Field(default=1, ge=1, le=100)


@router.post("/subscription/provision", status_code=201)
async def billing_sub_provision(
    body: SubscriptionProvisionRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """
    Directly provision an active subscription without Mollie — service-token only.

    Produces the same DB state as a completed Mollie checkout + webhook cycle
    (status='active', monthly budget provisioned) without initiating a payment.
    Intended for seeding test environments.

    Idempotent: if the user already has an active subscription for the given plan,
    returns it. Refuses trial plans (400) — those auto-provision via evaluate_budget.
    """
    try:
        return await billing_service.provision_subscription(
            body.userId, body.planId, body.seats
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/seed-legacy-trials/{app_id}")
async def billing_seed_legacy_trials(
    app_id: str,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """
    One-shot backfill: every user without an active subscription for `app_id`
    gets a 7-day trial. Service-token only. Idempotent — returns counts.

    Run this BEFORE tightening _BLOCKING_REASONS in src/budget/gate.py to
    include 'unlicensed', otherwise legacy users would be locked out.
    """
    if app_id not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"unknown app_id: {app_id}")
    return await billing_service.seed_legacy_trials(app_id)


@router.post("/subscription/checkout")
async def billing_sub_checkout(
    body: SubscriptionCheckoutRequest,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, str]:
    user_id, email, name = await _resolve_billing_identity(
        claims, body.userId, body.email, body.name,
    )
    try:
        return await billing_service.start_subscription_checkout(
            user_id, body.planId, body.seats, body.successRedirect,
            email, name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ProjectPackCheckoutRequest(BaseModel):
    planId: str
    quantity: int = 1
    successRedirect: str
    # Operator-Service-Token darf userId/email/name mitgeben; beim User-JWT
    # gewinnt die Auth-Identität (siehe _resolve_billing_identity).
    userId: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None


@router.post("/project-pack/checkout")
async def billing_project_pack_checkout(
    body: ProjectPackCheckoutRequest,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, str]:
    """
    Self-Service-Nachbestellung eines Projekt-Pakets (Mollie-Einmalzahlung).
    Nur Bestandskunden + vollständige Rechnungsadresse (Gate in billing_service).
    """
    user_id, email, name = await _resolve_billing_identity(
        claims, body.userId, body.email, body.name,
    )
    try:
        return await billing_service.start_project_pack_checkout(
            user_id, body.planId, body.quantity, body.successRedirect,
            email, name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class TopUpCheckoutRequest(BaseModel):
    # Top-Up bounds are enforced again in billing_service.start_topup_checkout
    # (defence in depth) — these Pydantic bounds reject obviously bad requests
    # before they hit business logic.
    amountEur: float = Field(ge=50, le=1000)
    successRedirect: str
    # userId/email/name: see SubscriptionCheckoutRequest above. Same defence-
    # in-depth rules — auth context wins, body is operator-mode fallback.
    userId: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None


@router.post("/topup/checkout")
async def billing_topup_checkout(
    body: TopUpCheckoutRequest,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, str]:
    user_id, email, name = await _resolve_billing_identity(
        claims, body.userId, body.email, body.name,
    )
    try:
        return await billing_service.start_topup_checkout(
            user_id, body.amountEur, body.successRedirect,
            email, name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/mollie-webhook")
async def billing_webhook(id: str = Form(...)) -> Dict[str, Any]:
    """
    Mollie webhook receiver.

    PUBLIC by design: Mollie calls us with HTTP Basic over Mollie's TLS. We
    do NOT trust the payload — every webhook firing re-fetches the payment
    state directly from Mollie's API inside `handle_webhook`. An attacker
    posting fake payment_ids cannot trick us because Mollie's API would
    return 404 or a payment we never registered.

    Error handling: we let unexpected exceptions bubble up to FastAPI's
    500-handler. Mollie will then retry per its delivery contract.
    Returning 200 with "error" silently dropped payments on partial DB
    failures — never again.

    Known-graceful conditions return 200 with {handled: False, reason: ...}
    so Mollie does not retry forever for payments we cannot process
    (unknown payment_id, status != 'paid').
    """
    return await billing_service.handle_webhook(id)


@router.get("/{user_id}/subscriptions")
async def billing_list_subs(
    user_id: str,
    _claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    return {"subscriptions": await billing_service.list_subscriptions(user_id)}


@router.get("/{user_id}/credit-purchases")
async def billing_list_credits(
    user_id: str,
    _claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    return {"creditPurchases": await billing_service.list_credit_purchases(user_id)}


class SubscriptionChangeRequest(BaseModel):
    userId: str
    email: str
    name: str
    newPlanId: str
    seats: int = Field(default=1, ge=1, le=100)
    successRedirect: str


@router.post("/subscription/change")
async def billing_sub_change(
    body: SubscriptionChangeRequest,
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    """Upgrade, downgrade, or reseat an active subscription.

    Cancels the current active subscription and starts a new checkout.
    The new subscription activates when the first payment completes.
    Returns the checkout URL and the ID of the cancelled subscription.
    """
    try:
        return await billing_service.change_subscription(
            body.userId, body.newPlanId, body.seats, body.successRedirect,
            body.email, body.name,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{user_id}/subscriptions/{sub_id}/cancel", status_code=204, response_class=Response)
async def billing_cancel_sub(
    user_id: str,
    sub_id: str,
    _claims: AuthClaims = Depends(require_self_or_admin),
) -> Response:
    try:
        await billing_service.cancel_subscription(user_id, sub_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Events — Mollie audit trail
# ---------------------------------------------------------------------------


@router.get("/events")
async def list_billing_events(
    userId: str | None = None,
    tenantId: str | None = None,
    eventType: str | None = None,
    account_type: str | None = None,
    limit: int = 200,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """Append-only billing audit trail. Admin only.

    account_type filters by the owning tenant's type (customer|test|internal)
    via an INNER JOIN on tenants. Events without a tenant_id are excluded when
    account_type is set — they cannot be attributed to an account type.
    """
    import uuid as _uuid

    if account_type and account_type not in _ALLOWED_ACCOUNT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid account_type: {account_type!r}. Must be customer|test|internal",
        )

    pool = get_pool()
    where: list = []
    args: list = []

    def add(cond: str, val: Any) -> None:
        args.append(val)
        where.append(cond.replace("$$", f"${len(args)}"))

    if account_type:
        # Qualify column names to avoid ambiguity with the tenants JOIN.
        if userId:
            add("be.user_id = $$", _uuid.UUID(userId))
        if tenantId:
            add("be.tenant_id = $$", tenantId)
        if eventType:
            add("be.event_type = $$", eventType)
        add("t.account_type = $$::account_type", account_type)
        sql = (
            "SELECT be.id, be.timestamp, be.event_type, be.user_id, be.tenant_id, "
            "be.subscription_id, be.invoice_id, be.mollie_payment_id, be.amount_eur, be.source, be.payload "
            "FROM billing_events be "
            "JOIN tenants t ON be.tenant_id = t.id"
        )
        order_col = "be.timestamp"
    else:
        if userId:
            add("user_id = $$", _uuid.UUID(userId))
        if tenantId:
            add("tenant_id = $$", tenantId)
        if eventType:
            add("event_type = $$", eventType)
        sql = (
            "SELECT id, timestamp, event_type, user_id, tenant_id, subscription_id, "
            "invoice_id, mollie_payment_id, amount_eur, source, payload FROM billing_events"
        )
        order_col = "timestamp"

    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order_col} DESC LIMIT ${len(args) + 1}"
    args.append(min(max(1, limit), 1000))

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    def _row(r):
        return {
            "id": str(r["id"]),
            "timestamp": r["timestamp"].isoformat(),
            "eventType": r["event_type"],
            "userId": str(r["user_id"]) if r["user_id"] else None,
            "tenantId": r["tenant_id"],
            "subscriptionId": str(r["subscription_id"]) if r["subscription_id"] else None,
            "invoiceId": str(r["invoice_id"]) if r["invoice_id"] else None,
            "molliePaymentId": r["mollie_payment_id"],
            "amountEur": float(r["amount_eur"]) if r["amount_eur"] is not None else None,
            "source": r["source"],
            "payload": r["payload"] if isinstance(r["payload"], dict) else {},
        }
    return {"items": [_row(r) for r in rows], "count": len(rows)}


# ---------------------------------------------------------------------------
# Pending-Orders — Rechnungs-Lane (Variante A: manuelle Freigabe)
# ---------------------------------------------------------------------------

from src.billing import pending_orders_service  # noqa: E402
from src.billing import project_credits_service  # noqa: E402
from src.billing import project_budgets_service  # noqa: E402

_pending_router = APIRouter(tags=["billing"])
_admin_orders_router = APIRouter(tags=["billing"])


class InvoiceOrderRequest(BaseModel):
    planId: str
    quantity: int = Field(default=1, ge=1, le=100)


@_pending_router.post("/v1/billing/order/invoice", status_code=201)
async def order_invoice(
    body: InvoiceOrderRequest,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    """
    Admin bestellt einen Plan auf Rechnung (manuelle Freigabe durch Operator).

    Erstellt Invoice (status='issued'), pending_orders-Row und sendet Email.
    Gated auf require_jwt_or_service — Auth wie andere customer-facing Billing-Endpoints.
    acting_user_id ist der bestellende Admin.
    """
    user_id = claims.acting_user_id
    if not user_id:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=401, detail="acting_user_id required — include X-User-ID or use user JWT")
    try:
        return await pending_orders_service.create_pending_order(
            user_id=user_id,
            plan_id=body.planId,
            quantity=body.quantity,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@_pending_router.get("/v1/users/{user_id}/pending-orders")
async def list_user_pending_orders(
    user_id: str,
    _claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    """Gibt alle Pending-Orders eines Users zurück (self-or-admin)."""
    return {"items": await pending_orders_service.list_user_pending_orders(user_id)}


@_admin_orders_router.get("/v1/admin/orders/pending")
async def list_pending_orders(
    status: Optional[str] = Query(
        default=None,
        description="Filter: awaiting_payment | released | expired | cancelled",
    ),
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """Alle Pending-Orders (operator only). Optional nach status filtern."""
    _VALID_STATUSES = {"awaiting_payment", "released", "expired", "cancelled"}
    if status and status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status filter: {status}")
    items = await pending_orders_service.list_all_pending_orders(status_filter=status)
    return {"items": items, "count": len(items)}


class ReleaseOrderRequest(BaseModel):
    note: Optional[str] = None


@_admin_orders_router.post("/v1/admin/orders/{order_id}/release")
async def release_pending_order(
    order_id: str,
    body: ReleaseOrderRequest,
    claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Gibt eine Pending-Order frei (operator only).

    Aktiviert Subscription, markiert Invoice als bezahlt, updated Order-Status.
    Idempotent auf DB-Ebene (ON CONFLICT). Bei bereits releastem Order → 409.
    """
    # Service-token operator has no user_id in the claim. Pass None so
    # release_order writes NULL into released_by — the release_note still
    # captures context. Hard-coding the literal string "operator" produced
    # UUID("operator") errors on cast inside release_order.
    operator_id = claims.user_id
    try:
        return await pending_orders_service.release_order(
            order_id=order_id,
            operator_user_id=operator_id,
            note=body.note,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))


# ---------------------------------------------------------------------------
# Project Credits — Slot-Zähler für Projekt-Plans (interval='project')
# ---------------------------------------------------------------------------

import uuid as _uuid

_project_credits_router = APIRouter(tags=["billing"])


@_project_credits_router.get("/v1/users/{user_id}/project-credits")
async def list_project_credits(
    user_id: str,
    _claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    """
    Verfügbare Projekt-Credits eines Users abrufen.

    Aggregiert alle manual_project_credits-Rows nach plan_id.
    Self-or-Admin: User kann eigene Credits sehen, Operator alle.
    """
    try:
        credits = await project_credits_service.list_user_credits(_uuid.UUID(user_id))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid user_id: {user_id}")
    return {"credits": credits}


@_project_credits_router.get("/v1/users/{user_id}/project-budgets")
async def list_project_budgets(
    user_id: str,
    plan_id: str = "energy-project",
    _claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    """
    Per-Projekt-Budgets eines Users (für die App-Budget-Anzeige).

    Liefert pro Projekt {projectId, limitEur, usedEur, remainingEur, tokensUsed}
    plus aggregierte totals (N Projekte × EUR 100 = gemeinsamer Topf). Tokens
    kommen aus usage_events (workflow_id == project_id). Self-or-Admin: User
    sieht eigene Budgets, Operator alle.
    """
    try:
        return await project_budgets_service.list_budgets(_uuid.UUID(user_id), plan_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid user_id: {user_id}")


class ConsumeProjectCreditRequest(BaseModel):
    # App-side project identifier (== Bridge attribution workflow_id). When
    # present, the consumed slot's per-project API budget is allocated atomically.
    project_id: Optional[str] = None


@_project_credits_router.post(
    "/v1/users/{user_id}/project-credits/{plan_id}/consume",
    status_code=200,
)
async def consume_project_credit(
    user_id: str,
    plan_id: str,
    body: Optional[ConsumeProjectCreditRequest] = None,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """
    1 Projekt-Credit für user_id+plan_id verbrauchen.

    Service-Token only (kein User-JWT): nur App-Backends dürfen Credits verbrauchen.
    Energy-App ruft diesen Endpoint server-seitig vor dem Railway-Backend-Call auf.
    Gibt 402 zurück wenn keine Credits verfügbar (CreditsExhaustedError).

    Optional `project_id` im JSON-Body allokiert das Per-Projekt-API-Budget
    atomar mit dem Slot. Ohne project_id (Alt-Clients) unverändert (nur Slot;
    Budget fällt downstream aufs Monatsbudget zurück). JSON-Body, damit Umlauts
    in project_id sauber durchgehen.
    """
    project_id = body.project_id if body else None
    try:
        return await project_credits_service.consume_credit(
            _uuid.UUID(user_id), plan_id, project_id
        )
    except project_credits_service.CreditsExhaustedError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid user_id: {user_id}")


# Export all routers so platform_main.py can include them.
pending_orders_router = _pending_router
admin_orders_router = _admin_orders_router
project_credits_router = _project_credits_router
