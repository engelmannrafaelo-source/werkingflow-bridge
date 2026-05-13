"""
BillingService — orchestriert Subscriptions + Top-Ups + Mollie + DB.

Webhook-Handler: Mollie ruft /v1/billing/mollie-webhook auf, wir branchen nach pending_payments.type.
- subscription_first -> Mollie-Subscription erstellen, in subscriptions persistieren
- topup -> credit_purchases persistieren, user_topup_balances erhoehen (gemeinsamer Pool!)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config import config
from src.db.client import get_pool
from src.billing.mollie_adapter import get_mollie_adapter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

def _serialize_customer(row: Any) -> Dict[str, Any]:
    return {
        "userId": str(row["user_id"]),
        "mollieCustomerId": row["mollie_customer_id"],
        "email": row["email"],
        "name": row["name"],
        "createdAt": row["created_at"].isoformat(),
    }


async def get_or_create_customer(user_id: str, email: str, name: str) -> Dict[str, Any]:
    """
    Idempotent customer provisioning.

    The lookup→Mollie-create→INSERT used to span three DB acquisitions with a
    network call in between (TOCTOU race C4). Two concurrent callers for the
    same user could each pass the SELECT, each call Mollie (creating two
    Mollie customers), then race the INSERT — one would orphan its Mollie
    customer. We now hold a single connection and use INSERT … ON CONFLICT
    DO NOTHING RETURNING; if the conflict path fires (another writer won),
    we re-SELECT inside the same connection.
    """
    pool = get_pool()
    user_uuid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT user_id, mollie_customer_id, email, name, created_at FROM mollie_customers WHERE user_id = $1",
            user_uuid,
        )
        if existing:
            return _serialize_customer(existing)

        # No customer yet. Call Mollie OUTSIDE the implicit transaction so
        # we don't hold a row lock during the network call. The INSERT
        # below uses ON CONFLICT to handle the race where a concurrent
        # caller created the row while we were waiting on Mollie.
        mollie = get_mollie_adapter()
        mollie_id = await mollie.create_customer(email=email, name=name)

        row = await conn.fetchrow(
            """
            INSERT INTO mollie_customers (user_id, mollie_customer_id, email, name, created_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (user_id) DO NOTHING
            RETURNING user_id, mollie_customer_id, email, name, created_at
            """,
            user_uuid, mollie_id, email, name,
        )
        if row:
            return _serialize_customer(row)

        # Conflict: another writer raced us. Use the row they wrote.
        # The orphaned Mollie customer we just created stays in Mollie's
        # data — we don't attempt cleanup because that opens new race
        # surfaces; Mollie customers are cheap and idempotent on email.
        winner = await conn.fetchrow(
            "SELECT user_id, mollie_customer_id, email, name, created_at FROM mollie_customers WHERE user_id = $1",
            user_uuid,
        )
        if not winner:
            # Should never happen: ON CONFLICT fired but no row exists.
            raise RuntimeError(
                f"get_or_create_customer: race resolution failed for user {user_id}"
            )
        return _serialize_customer(winner)


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

def _plan_price(plan_id: str) -> float:
    """Source-of-truth: src/budget/plans.py. No local duplicate."""
    from src.budget.plans import get_plan
    return float(get_plan(plan_id).price)


async def start_subscription_checkout(
    user_id: str, plan_id: str, seats: int, success_redirect: str,
    email: str, name: str,
) -> Dict[str, str]:
    customer = await get_or_create_customer(user_id, email, name)
    amount = _plan_price(plan_id) * seats

    mollie = get_mollie_adapter()
    checkout = await mollie.create_first_payment(
        customer_id=customer["mollieCustomerId"],
        amount_eur=amount,
        description=f"{plan_id} ({seats} Sitz{'e' if seats > 1 else ''})",
        redirect_url=success_redirect,
        webhook_url=config.mollie_webhook_url,
        metadata={
            "type": "subscription_first",
            "userId": user_id,
            "planId": plan_id,
            "seats": str(seats),
        },
    )

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_payments (payment_id, user_id, type, plan_id, amount_eur, created_at)
            VALUES ($1, $2, 'subscription_first', $3, $4, NOW())
            ON CONFLICT (payment_id) DO NOTHING
            """,
            checkout["paymentId"], uuid.UUID(user_id), plan_id, amount,
        )
    return checkout


async def _activate_subscription(
    user_id: str, plan_id: str, seats: int, customer_id: str,
    first_payment_id: str,
) -> Dict[str, Any]:
    """
    Idempotent subscription activation.

    Mollie retries the webhook POST until it gets a 200. Without an
    idempotency guard, every retry would create another 'active' row.
    `subscriptions.mollie_first_payment_id` is UNIQUE: a retry finds the
    existing row and returns it untouched. Mollie's create_subscription
    call is only made on the first webhook firing — after the row exists,
    we return the persisted state.
    """
    pool = get_pool()

    # Idempotency probe FIRST. If we already activated this payment, return
    # the existing row without calling Mollie a second time.
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT id, user_id, app_id, plan_id, status, mollie_customer_id,
                   mollie_subscription_id, seats, started_at, cancelled_at
            FROM subscriptions WHERE mollie_first_payment_id = $1
            """,
            first_payment_id,
        )
        if existing:
            return _serialize_subscription(existing)

    # First-time activation. Call Mollie to create the recurring subscription.
    amount = _plan_price(plan_id) * seats
    mollie = get_mollie_adapter()
    sub_resp = await mollie.create_subscription(
        customer_id=customer_id,
        amount_eur=amount,
        interval="1 month",
        description=f"{plan_id} ({seats} Sitz{'e' if seats > 1 else ''})",
        webhook_url=config.mollie_webhook_url,
        metadata={"userId": user_id, "planId": plan_id},
    )

    # Map plan -> app (mirrors plans.py PLANS[*].app_id but kept local for
    # speed; should diverge only when a plan_id intentionally serves multiple
    # apps, which the schema does not currently allow).
    from src.budget.plans import get_plan
    app_id = get_plan(plan_id).app_id

    sub_uuid = uuid.uuid4()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO subscriptions
              (id, user_id, app_id, plan_id, status,
               mollie_customer_id, mollie_subscription_id, mollie_first_payment_id,
               seats, started_at)
            VALUES ($1, $2, $3, $4, 'active', $5, $6, $7, $8, NOW())
            ON CONFLICT (mollie_first_payment_id) DO NOTHING
            RETURNING id, user_id, app_id, plan_id, status, mollie_customer_id,
                      mollie_subscription_id, seats, started_at, cancelled_at
            """,
            sub_uuid, uuid.UUID(user_id), app_id, plan_id,
            customer_id, sub_resp["subscriptionId"], first_payment_id, seats,
        )
        if row:
            return _serialize_subscription(row)

        # Conflict: another webhook delivery raced us between the probe
        # and the insert. Return the row the winner wrote.
        winner = await conn.fetchrow(
            """
            SELECT id, user_id, app_id, plan_id, status, mollie_customer_id,
                   mollie_subscription_id, seats, started_at, cancelled_at
            FROM subscriptions WHERE mollie_first_payment_id = $1
            """,
            first_payment_id,
        )
        if not winner:
            raise RuntimeError(
                f"_activate_subscription: ON CONFLICT fired but no row found "
                f"for first_payment_id={first_payment_id}"
            )
        return _serialize_subscription(winner)


async def list_subscriptions(user_id: str) -> List[Dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, app_id, plan_id, status, mollie_customer_id, mollie_subscription_id,
                   seats, started_at, cancelled_at
            FROM subscriptions WHERE user_id = $1 ORDER BY started_at DESC
            """,
            uuid.UUID(user_id),
        )
    return [_serialize_subscription(r) for r in rows]


async def cancel_subscription(user_id: str, subscription_id: str) -> None:
    """
    Cancel a subscription transactionally.

    The Mollie cancel call and the DB UPDATE run inside a single
    transaction that holds FOR UPDATE on the subscription row. If the
    Mollie call raises, the transaction rolls back and the DB state
    remains 'active' — Mollie will continue charging, but admin views
    will not lie about the status. Concurrent cancel requests serialise
    on the row lock; the second one observes status='cancelled' and
    becomes a no-op.

    Failure mode we explicitly accept: Mollie cancel succeeds, then the
    DB commit fails (e.g. connection drop). Mollie stops charging, DB
    keeps 'active'. We treat that as a recoverable inconsistency — an
    admin retry of cancel finds Mollie already cancelled (idempotent on
    Mollie's side) and the DB UPDATE completes. The opposite ordering
    (DB cancel, Mollie still charges) would be a billing leak; we forbid
    it by calling Mollie BEFORE the DB UPDATE inside the transaction.
    """
    pool = get_pool()
    sub_uuid = uuid.UUID(subscription_id)
    user_uuid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT mollie_customer_id, mollie_subscription_id, status
                FROM subscriptions WHERE id = $1 AND user_id = $2 FOR UPDATE
                """,
                sub_uuid, user_uuid,
            )
            if not row:
                raise LookupError(
                    f"Subscription {subscription_id} not found for user {user_id}"
                )
            if row["status"] == "cancelled":
                # Already cancelled — concurrent request lost the race. No-op.
                return

            if row["mollie_subscription_id"]:
                mollie = get_mollie_adapter()
                await mollie.cancel_subscription(
                    row["mollie_customer_id"], row["mollie_subscription_id"]
                )

            await conn.execute(
                "UPDATE subscriptions SET status='cancelled', cancelled_at=NOW() WHERE id=$1",
                sub_uuid,
            )


# ---------------------------------------------------------------------------
# Top-Up (gemeinsamer Pool!)
# ---------------------------------------------------------------------------

async def start_topup_checkout(
    user_id: str, amount_eur: float, success_redirect: str,
    email: str, name: str,
) -> Dict[str, str]:
    if amount_eur < 50 or amount_eur > 1000:
        raise ValueError(f"Top-Up amount EUR {amount_eur} out of range [50, 1000]")

    customer = await get_or_create_customer(user_id, email, name)
    mollie = get_mollie_adapter()
    checkout = await mollie.create_one_time_payment(
        customer_id=customer["mollieCustomerId"],
        amount_eur=amount_eur,
        description=f"API-Top-Up EUR {amount_eur}",
        redirect_url=success_redirect,
        webhook_url=config.mollie_webhook_url,
        metadata={"type": "topup", "userId": user_id},
    )

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_payments (payment_id, user_id, type, amount_eur, created_at)
            VALUES ($1, $2, 'topup', $3, NOW())
            ON CONFLICT (payment_id) DO NOTHING
            """,
            checkout["paymentId"], uuid.UUID(user_id), amount_eur,
        )
    return checkout


async def _credit_topup(user_id: str, amount_eur: float, mollie_payment_id: str) -> Dict[str, Any]:
    pool = get_pool()
    user_uuid = uuid.UUID(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            cust = await conn.fetchrow(
                "SELECT mollie_customer_id FROM mollie_customers WHERE user_id = $1",
                user_uuid,
            )
            if not cust:
                raise LookupError(f"No Mollie customer for user {user_id}")

            # Idempotency: wenn schon credit_purchase fuer payment_id existiert, skip
            existing = await conn.fetchval(
                "SELECT id FROM credit_purchases WHERE mollie_payment_id = $1",
                mollie_payment_id,
            )
            if existing:
                # Hole aktuellen Balance ohne nochmal zu erhoehen
                balance = await conn.fetchval(
                    "SELECT balance_eur FROM user_topup_balances WHERE user_id = $1",
                    user_uuid,
                )
                return {"alreadyCredited": True, "balanceEur": float(balance or 0)}

            await conn.execute(
                """
                INSERT INTO credit_purchases (id, user_id, pack_eur, mollie_customer_id, mollie_payment_id, paid_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                """,
                uuid.uuid4(), user_uuid, amount_eur, cust["mollie_customer_id"], mollie_payment_id,
            )
            new_balance = await conn.fetchval(
                """
                INSERT INTO user_topup_balances (user_id, balance_eur, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (user_id) DO UPDATE
                  SET balance_eur = user_topup_balances.balance_eur + EXCLUDED.balance_eur,
                      updated_at = NOW()
                RETURNING balance_eur
                """,
                user_uuid, amount_eur,
            )
    return {"alreadyCredited": False, "balanceEur": float(new_balance)}


async def list_credit_purchases(user_id: str) -> List[Dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, pack_eur, mollie_customer_id, mollie_payment_id, paid_at
            FROM credit_purchases WHERE user_id = $1 ORDER BY paid_at DESC
            """,
            uuid.UUID(user_id),
        )
    return [
        {
            "id": str(r["id"]),
            "userId": str(r["user_id"]),
            "amountEur": float(r["pack_eur"]),
            "mollieCustomerId": r["mollie_customer_id"],
            "molliePaymentId": r["mollie_payment_id"],
            "paidAt": r["paid_at"].isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Webhook (Mollie ruft auf)
# ---------------------------------------------------------------------------

async def handle_webhook(payment_id: str) -> Dict[str, Any]:
    mollie = get_mollie_adapter()
    payment = await mollie.get_payment(payment_id)
    if payment.get("status") != "paid":
        return {"handled": False, "reason": f"status={payment.get('status')}"}

    pool = get_pool()
    async with pool.acquire() as conn:
        pending = await conn.fetchrow(
            "SELECT user_id, type, plan_id, amount_eur FROM pending_payments WHERE payment_id = $1",
            payment_id,
        )
    if not pending:
        return {"handled": False, "reason": "unknown payment_id"}

    user_id = str(pending["user_id"])
    if pending["type"] == "topup":
        result = await _credit_topup(user_id, float(pending["amount_eur"]), payment_id)
        return {"handled": True, "type": "topup", "result": result}

    if pending["type"] == "subscription_first":
        seats = int(payment.get("metadata", {}).get("seats", 1))
        cust = payment.get("customer_id")
        if not cust:
            # FakeMollie: customer_id steckt im stored payment
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT mollie_customer_id FROM mollie_customers WHERE user_id = $1",
                    uuid.UUID(user_id),
                )
            cust = row["mollie_customer_id"] if row else None
        if not cust:
            raise RuntimeError("subscription_first: customer_id missing")
        sub = await _activate_subscription(
            user_id, pending["plan_id"], seats, cust, first_payment_id=payment_id,
        )
        return {"handled": True, "type": "subscription_first", "subscription": sub}

    return {"handled": False, "reason": f"unknown type {pending['type']}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_subscription(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "userId": str(row["user_id"]),
        "appId": row["app_id"],
        "planId": row["plan_id"],
        "status": row["status"],
        "mollieCustomerId": row["mollie_customer_id"],
        "mollieSubscriptionId": row["mollie_subscription_id"],
        "seats": row["seats"],
        "startedAt": row["started_at"].isoformat() if row["started_at"] else None,
        "cancelledAt": row["cancelled_at"].isoformat() if row["cancelled_at"] else None,
    }
