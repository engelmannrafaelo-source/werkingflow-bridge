"""
Billing-Endpoints — Cross-App Mollie-Integration.

Auth model:
  - /v1/billing/mollie-webhook       PUBLIC (Mollie calls us; we verify by re-fetching payment from Mollie)
  - /v1/billing/customer             require_jwt_or_service
  - /v1/billing/subscription/...     require_jwt_or_service
  - /v1/billing/topup/...            require_jwt_or_service
  - /v1/billing/{user_id}/...        require_self_or_admin
"""
from typing import Any, Dict

from fastapi import APIRouter, Depends, Form, HTTPException, Response
from pydantic import BaseModel, Field

from src.billing import billing_service
from src.api_auth import require_jwt_or_service, require_self_or_admin, AuthClaims

router = APIRouter(prefix="/v1/billing", tags=["billing"])


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
