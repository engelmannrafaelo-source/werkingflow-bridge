"""
Billing-Endpoints — Cross-App Mollie-Integration.

Auth model:
  - /v1/billing/mollie-webhook       PUBLIC (Mollie calls us; we verify by re-fetching payment from Mollie)
  - /v1/billing/customer             require_jwt_or_service
  - /v1/billing/subscription/...     require_jwt_or_service
  - /v1/billing/topup/...            require_jwt_or_service
  - /v1/billing/{user_id}/...        require_self_or_admin
"""
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, Form, HTTPException, Response
from pydantic import BaseModel, Field

from src.billing import billing_service
from src.api_auth import require_admin, require_jwt_or_service, require_self_or_admin, AuthClaims
from src.budget.plans import PLANS
from src.db.client import get_pool

router = APIRouter(prefix="/v1/billing", tags=["billing"])


# ---------------------------------------------------------------------------
# Overview — admin dashboard cross-tenant aggregates
# ---------------------------------------------------------------------------

@router.get("/overview")
async def billing_overview(
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
    """
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

    # Top-up totals
    async with pool.acquire() as conn:
        topup_sum_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(pack_eur), 0) AS total, COUNT(*) AS count FROM credit_purchases"
        )
        topup_balances_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(balance_eur), 0) AS total FROM user_topup_balances"
        )

    return {
        "source": source,
        "totalMrrEur": round(total_mrr_eur, 2),
        "activeSubscriptions": active_subs,
        "cancelledSubscriptions": cancelled_subs,
        "totalTopupRevenueEur": float(topup_sum_row["total"]),
        "topupPurchasesCount": int(topup_sum_row["count"]),
        "totalUserBalanceEur": float(topup_balances_row["total"]),
        "byPlan": by_plan,
        "byStatus": by_status,
        "byApp": by_app,
    }


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


class SubscriptionCheckoutRequest(BaseModel):
    userId: str
    email: str
    name: str
    planId: str
    # seats must be a positive small integer. Above 100 we suspect API abuse —
    # a single buyer would never legitimately seat 100 users at once via this
    # endpoint. Tighten further once the real cap is known.
    seats: int = Field(default=1, ge=1, le=100)
    successRedirect: str


@router.post("/subscription/checkout")
async def billing_sub_checkout(
    body: SubscriptionCheckoutRequest,
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, str]:
    try:
        return await billing_service.start_subscription_checkout(
            body.userId, body.planId, body.seats, body.successRedirect,
            body.email, body.name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class TopUpCheckoutRequest(BaseModel):
    userId: str
    email: str
    name: str
    # Top-Up bounds are enforced again in billing_service.start_topup_checkout
    # (defence in depth) — these Pydantic bounds reject obviously bad requests
    # before they hit business logic.
    amountEur: float = Field(ge=50, le=1000)
    successRedirect: str


@router.post("/topup/checkout")
async def billing_topup_checkout(
    body: TopUpCheckoutRequest,
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, str]:
    try:
        return await billing_service.start_topup_checkout(
            body.userId, body.amountEur, body.successRedirect,
            body.email, body.name,
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


@router.post("/{user_id}/subscriptions/{sub_id}/cancel", status_code=204)
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
    limit: int = 200,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """Append-only billing audit trail. Admin only."""
    import uuid as _uuid
    from typing import List, Any
    pool = get_pool()
    where: list = []
    args: list = []
    def add(cond: str, val: Any) -> None:
        args.append(val); where.append(cond.replace("$$", f"${len(args)}"))
    if userId:    add("user_id = $$", _uuid.UUID(userId))
    if tenantId:  add("tenant_id = $$", tenantId)
    if eventType: add("event_type = $$", eventType)

    sql = "SELECT id, timestamp, event_type, user_id, tenant_id, subscription_id, invoice_id, mollie_payment_id, amount_eur, source, payload FROM billing_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY timestamp DESC LIMIT ${len(args) + 1}"
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

