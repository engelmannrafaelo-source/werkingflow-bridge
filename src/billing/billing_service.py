"""
BillingService — orchestriert Subscriptions + Top-Ups + Mollie + DB.

Webhook-Handler: Mollie ruft /v1/billing/mollie-webhook auf, wir branchen nach pending_payments.type.
- subscription_first -> Mollie-Subscription erstellen, in subscriptions persistieren
- topup -> credit_purchases persistieren, user_topup_balances erhoehen (gemeinsamer Pool!)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.config import config
from src.db.client import get_pool
from src.billing.mollie_adapter import get_mollie_adapter


# ---------------------------------------------------------------------------
# Tax-rate determination — based on recipient billing address
# ---------------------------------------------------------------------------

# EU member states, ISO 3166-1 alpha-2.  AT is excluded: it uses the domestic
# rate and needs no Reverse Charge note.
_EU_COUNTRIES_NON_AT: frozenset[str] = frozenset({
    "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT",
    "NL", "PL", "PT", "RO", "SE", "SI", "SK",
})


def _determine_tax_rate(
    billing_address: Optional[Dict[str, Any]],
) -> Tuple[float, Optional[str]]:
    """Return (tax_rate_percent, reverse_charge_note_or_None).

    Rules:
    - No address or no country → 20.0 AT domestic (safe default, explicit)
    - AT → 20.0 (domestic USt)
    - EU (non-AT) + vatId present → 0.0, Reverse Charge (B2B)
    - EU (non-AT) without vatId → 20.0 AT (safe default)
      OPEN DECISION: EU B2C falls under the OSS-Verfahren; correct rate per
      destination country is a legal question not answered here.  We use 20%
      AT as the conservative safe default and mark the invoice metadata with
      {"taxNote": "EU_B2C_OSS_OPEN"} so operators can identify these cases.
      This is intentional and must be resolved with a tax advisor before
      serving significant EU B2C volume.
    - Non-EU → 0.0 (export, no VAT)

    VIES validation of the vatId is NOT performed here.  A non-empty vatId is
    treated as a B2B signal; the issuer remains responsible for record-keeping
    under § 18 UStG.
    """
    if not billing_address:
        return 20.0, None

    country = (billing_address.get("country") or "").upper().strip()
    vat_id = (billing_address.get("vatId") or "").strip()

    if not country:
        return 20.0, None

    if country == "AT":
        return 20.0, None

    if country in _EU_COUNTRIES_NON_AT:
        if vat_id:
            return 0.0, (
                "Steuerschuldnerschaft des Leistungsempfängers "
                "gem. § 19 Abs. 1 UStG (Reverse Charge)"
            )
        # EU B2C — OSS open decision, conservative AT default
        return 20.0, None

    # Non-EU export
    return 0.0, None


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
            await log_billing_event(
                "customer.created",
                user_id=user_id,
                source="system",
                payload={"mollieCustomerId": str(row["mollie_customer_id"]), "email": email, "name": name},
            )
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
    # Fail-fast: refuse the Mollie round-trip if the tenant's billing address
    # is incomplete. Without this gate, a user could complete payment on
    # Mollie and then provision_subscription (the webhook handler) would
    # reject the activation because of missing fields — leaving them paid
    # without an active subscription. Routes catch ValueError and translate
    # to 422 with a missing_fields detail, matching the existing webhook
    # error shape. Trial plan does not need this gate, but the FE never
    # checkouts trial; defence-in-depth.
    pool = get_pool()
    async with pool.acquire() as conn:
        await _assert_complete_billing_address(conn, uuid.UUID(user_id), plan_id)

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
            await log_billing_event(
                "subscription.activated",
                user_id=user_id,
                subscription_id=str(row["id"]),
                mollie_payment_id=first_payment_id,
                amount_eur=float(amount),
                source="mollie-webhook",
                payload={"planId": plan_id, "seats": seats},
            )
            await auto_create_invoice(
                user_id=user_id,
                amount_eur=float(amount),
                description=f"{plan_id} — Erste Monatsabrechnung ({seats} Sitz" + ("e" if seats > 1 else "") + ")",
                subscription_id=str(row["id"]),
                mollie_payment_id=first_payment_id,
            )
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
    """
    Returns the user's subscriptions, lazy-expiring any active trial whose
    trial_ends_at has passed. Lazy because:
      * Forced-trial cohorts are small (one row per registered user) so the
        UPDATE-on-read overhead is negligible vs. running a cron-job.
      * Lazy keeps the expiry state correct *for the caller's view* even
        if a backstop scheduler missed a window — no stale UI possible.
    A scheduled job would still be a useful belt-and-suspenders defense for
    apps that don't call list_subscriptions on every request (planned: see
    follow-up issue). For now this is the single load-bearing expire path.

    The UPDATE runs in a separate transaction-friendly statement BEFORE
    the SELECT so the returned rows already reflect the new state. A
    concurrent caller racing the same UPDATE is safe: the WHERE clause
    re-matches only rows still in the past-due state, the second writer
    just no-ops.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # Lazy expiry — UPDATE first so the SELECT below sees the new state.
        await conn.execute(
            """
            UPDATE subscriptions
            SET status = 'expired'::subscription_status,
                expired_at = NOW()
            WHERE user_id = $1
              AND plan_id = 'trial'
              AND status = 'active'
              AND trial_ends_at IS NOT NULL
              AND trial_ends_at < NOW()
            """,
            uuid.UUID(user_id),
        )
        rows = await conn.fetch(
            """
            SELECT id, user_id, app_id, plan_id, status, mollie_customer_id, mollie_subscription_id,
                   seats, started_at, cancelled_at, suspended_at, expired_at, trial_ends_at
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
            await log_billing_event(
                "subscription.cancelled",
                user_id=user_id,
                subscription_id=subscription_id,
                source="admin",
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
    await log_billing_event(
        "topup.credited",
        user_id=user_id,
        mollie_payment_id=mollie_payment_id,
        amount_eur=amount_eur,
        source="mollie-webhook",
        payload={"newBalanceEur": float(new_balance)},
    )
    await auto_create_invoice(
        user_id=user_id,
        amount_eur=amount_eur,
        description=f"API-Top-Up € {amount_eur:.2f}",
        mollie_payment_id=mollie_payment_id,
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

async def _record_failed_payment(payment_id: str, status: str) -> None:
    """Append a billing_event for a Mollie payment that will never complete.

    No state rollback is needed — subscription / topup activation only ever
    runs on status == "paid" — but a lost payment must stay auditable.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        pending = await conn.fetchrow(
            "SELECT user_id, type, plan_id, amount_eur FROM pending_payments WHERE payment_id = $1",
            payment_id,
        )
    if not pending:
        # Not one of ours, or the pending row is already gone — nothing to attribute.
        return
    await log_billing_event(
        "payment.failed",
        user_id=str(pending["user_id"]),
        mollie_payment_id=payment_id,
        amount_eur=float(pending["amount_eur"]) if pending["amount_eur"] is not None else None,
        source="mollie-webhook",
        payload={"status": status, "type": pending["type"], "planId": pending["plan_id"]},
    )


async def _suspend_subscription_by_mollie_id(
    mollie_subscription_id: str,
    payment_id: str,
    status: str,
) -> None:
    """Suspend an active subscription when its recurring Mollie payment fails.

    Mollie fires the webhook for each payment in a subscription — including
    recurring monthly ones.  Those payments are NOT in pending_payments (only
    the first payment is), so handle_webhook falls through here.  We look up
    the subscription by mollie_subscription_id; if it is 'active' we flip it
    to 'suspended' and leave an audit trail.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE subscriptions
               SET status = 'suspended', suspended_at = NOW()
             WHERE mollie_subscription_id = $1 AND status = 'active'
            RETURNING id, user_id
            """,
            mollie_subscription_id,
        )
    if row:
        await log_billing_event(
            "subscription.suspended",
            user_id=str(row["user_id"]),
            subscription_id=str(row["id"]),
            mollie_payment_id=payment_id,
            source="mollie-webhook",
            payload={"reason": f"recurring_payment_{status}"},
        )


async def expire_subscription_for_user_plan(user_id: str, plan_id: str) -> None:
    """Mark the user's active subscription for plan_id as 'expired'.

    Called when a trial's budget window closes (trial_expired in evaluate_budget).
    A no-op when no active subscription row exists (trials provisioned purely
    through user_budgets have no subscription row).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE subscriptions
               SET status = 'expired', expired_at = NOW()
             WHERE user_id = $1 AND plan_id = $2::plan_id AND status = 'active'
            RETURNING id
            """,
            uuid.UUID(user_id),
            plan_id,
        )
    if row:
        await log_billing_event(
            "subscription.expired",
            user_id=user_id,
            subscription_id=str(row["id"]),
            source="budget-check",
            payload={"planId": plan_id, "reason": "trial_period_ended"},
        )


async def change_subscription(
    user_id: str,
    new_plan_id: str,
    seats: int,
    success_redirect: str,
    email: str,
    name: str,
) -> Dict[str, Any]:
    """Upgrade, downgrade, or reseat a subscription.

    Cancels the current active subscription (Mollie + DB) and immediately
    starts a checkout for the new plan.  The new subscription is activated
    when the first payment completes via the Mollie webhook.

    Returns the checkout URL together with the ID of the cancelled subscription
    so the caller can communicate the transition to the user.

    Raises:
      LookupError  — no active subscription found for the user.
      ValueError   — new plan/seats are identical to the current ones (no-op guard).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        sub = await conn.fetchrow(
            """
            SELECT id, plan_id, seats, mollie_customer_id, mollie_subscription_id
              FROM subscriptions
             WHERE user_id = $1 AND status = 'active'
             ORDER BY started_at DESC
             LIMIT 1
            """,
            uuid.UUID(user_id),
        )

    if not sub:
        raise LookupError(f"No active subscription for user {user_id}")

    current_plan = sub["plan_id"]
    current_seats = sub["seats"]
    if current_plan == new_plan_id and current_seats == seats:
        raise ValueError(
            f"New plan ({new_plan_id}, {seats} seats) identical to current — nothing to change"
        )

    # Validate the new plan exists (raises ValueError for unknown).
    _plan_price(new_plan_id)

    sub_id = str(sub["id"])
    await cancel_subscription(user_id, sub_id)
    checkout = await start_subscription_checkout(
        user_id, new_plan_id, seats, success_redirect, email, name,
    )
    return {
        "cancelledSubscriptionId": sub_id,
        "previousPlanId": current_plan,
        "newPlanId": new_plan_id,
        "seats": seats,
        "checkoutUrl": checkout.get("checkoutUrl"),
        "paymentId": checkout.get("paymentId"),
    }


async def handle_webhook(payment_id: str) -> Dict[str, Any]:
    mollie = get_mollie_adapter()
    payment = await mollie.get_payment(payment_id)
    status = payment.get("status")
    if status != "paid":
        # open / pending / authorized are non-terminal — Mollie fires the
        # webhook again once the payment settles, so there is nothing to do.
        # Terminal failures (failed / expired / canceled) get an audit row:
        # activation is gated on status == "paid" so no state is corrupted,
        # but a lost payment must never vanish silently from the trail.
        if status in ("failed", "expired", "canceled"):
            await _record_failed_payment(payment_id, status)
            # Recurring subscription payments are NOT in pending_payments.
            # When such a payment fails, suspend the linked subscription.
            mollie_sub_id = payment.get("subscription_id")
            if mollie_sub_id:
                await _suspend_subscription_by_mollie_id(mollie_sub_id, payment_id, status)
        return {"handled": False, "reason": f"status={status}"}

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
    def _ts(col: str) -> Optional[str]:
        v = row[col] if col in row.keys() else None
        return v.isoformat() if v else None

    return {
        "id": str(row["id"]),
        "userId": str(row["user_id"]),
        "appId": row["app_id"],
        "planId": row["plan_id"],
        "status": row["status"],
        "mollieCustomerId": row["mollie_customer_id"],
        "mollieSubscriptionId": row["mollie_subscription_id"],
        "seats": row["seats"],
        "startedAt": _ts("started_at"),
        "cancelledAt": _ts("cancelled_at"),
        "suspendedAt": _ts("suspended_at"),
        "expiredAt": _ts("expired_at"),
        "trialEndsAt": _ts("trial_ends_at"),
    }


# ---------------------------------------------------------------------------
# Billing-event audit trail
# ---------------------------------------------------------------------------

async def log_billing_event(
    event_type: str,
    *,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
    invoice_id: Optional[str] = None,
    mollie_payment_id: Optional[str] = None,
    amount_eur: Optional[float] = None,
    source: str = "system",
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Append a row to billing_events. Append-only — never updates.

    tenant_id falls back to the user's tenant when not given explicitly, so
    billing events stay mode-filterable even if a caller forgets it. A truly
    user-less system event (no user_id, no tenant_id) is allowed — those are
    global and not tenant-scoped. See ADR 0007.
    """
    import json as _json
    if tenant_id is None and user_id:
        from src.api_auth.tenant_resolver import resolve_tenant_for_user
        tenant_id = await resolve_tenant_for_user(user_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO billing_events
              (event_type, user_id, tenant_id, subscription_id, invoice_id,
               mollie_payment_id, amount_eur, source, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            """,
            event_type,
            uuid.UUID(user_id) if user_id else None,
            tenant_id,
            uuid.UUID(subscription_id) if subscription_id else None,
            uuid.UUID(invoice_id) if invoice_id else None,
            mollie_payment_id,
            amount_eur,
            source,
            _json.dumps(payload or {}),
        )


# ---------------------------------------------------------------------------
# Direct subscription provisioning — for seed/test environments (no Mollie).
# ---------------------------------------------------------------------------

async def _provision_plan_budget(
    conn: Any, user_id: uuid.UUID, plan: Any
) -> None:
    """Insert a monthly budget entry for a paid plan — idempotent.

    Same guard as _provision_trial: the ON CONFLICT UPDATE fires only when the
    plan key is absent, so existing usage is never reset by a re-seed.
    Reset window: 30 days (standard monthly billing cycle).
    """
    valid_until = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    entry_json = json.dumps({
        plan.id: {
            "limitEur": float(plan.api_budget_eur),
            "usedEur": 0.0,
            "resetAt": valid_until,
        }
    })
    await conn.execute(
        """
        INSERT INTO user_budgets (user_id, monthly_budgets, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET monthly_budgets = user_budgets.monthly_budgets || $2::jsonb,
            updated_at = NOW()
        WHERE (user_budgets.monthly_budgets -> $3) IS NULL
        """,
        user_id,
        entry_json,
        plan.id,
    )


# Fields required on tenants.billing_* for a legally issuable invoice. vat_id
# stays optional — B2C customers (no UID) are explicitly allowed by §11 UStG.
# Keep this list in sync with migration 010_tenant_billing_address.sql comment.
_REQUIRED_BILLING_ADDRESS_FIELDS = ("billing_name", "billing_street", "billing_city",
                                     "billing_postcode", "billing_country")


# Exempt from the billing-address gate. Per the required-fields-yaml SSoT
# (packages/api-validation/required-fields.yaml stage_exemptions):
#   - 'test'     — seeded test tenants, fill defaults via seed-bridge-users
#                  Phase 2b but a re-seed against a freshly-migrated DB still
#                  needs to provision subscriptions BEFORE Phase 2b runs.
#   - 'internal' — internal tenants (employees, partners) bypass the customer
#                  lifecycle entirely.
# Only account_type='customer' goes through the gate. See the research
# baseline in docs/research/registration-required-fields-AT-EU-20260523.md
# for the legal reasoning (§11 UStG applies only to invoiceable B2B sales).
_BILLING_ADDRESS_GATE_EXEMPT_ACCOUNT_TYPES = frozenset({"test", "internal"})


async def _assert_complete_billing_address(
    conn: Any, user_uuid: uuid.UUID, plan_id: str
) -> None:
    """
    Refuse to proceed if a CUSTOMER tenant lacks any field that an Austrian
    invoice legally requires (§11 UStG: name + full address). The check runs
    at the subscription-provision boundary so an "active subscription without
    invoice-able state" never exists — defensive, fail-fast.

    Two-tier exemption (per packages/api-validation/required-fields.yaml):
      1. Trial plans skip the gate — trials produce no invoice, so §11
         doesn't bite. The address will be required at upgrade-to-paid time
         via the Mollie checkout flow.
      2. Tenants with account_type in {test, internal} skip the gate —
         test seeders + internal accounts bypass the customer lifecycle.

    For account_type='customer' on a non-trial plan: raises ValueError
    (-> 400 at the route layer) listing the missing fields so the caller
    knows exactly what to backfill via PATCH
    /v1/tenants/{tid}/billing-address.
    """
    # Trial plans are exempt — checked first to avoid a DB round-trip.
    from src.budget.plans import get_plan
    if get_plan(plan_id).trial:
        return

    row = await conn.fetchrow(
        """
        SELECT t.id           AS tenant_id,
               t.account_type::text AS account_type,
               t.billing_name, t.billing_street, t.billing_city,
               t.billing_postcode, t.billing_country
        FROM users u
        JOIN tenants t ON t.id = u.tenant_id
        WHERE u.id = $1
        """,
        user_uuid,
    )
    if row is None:
        raise ValueError(
            f"provision_subscription: user '{user_uuid}' has no tenant — "
            "cannot determine billing address"
        )

    # Test + internal tenants are exempt — see exemption-set comment above.
    if row["account_type"] in _BILLING_ADDRESS_GATE_EXEMPT_ACCOUNT_TYPES:
        return

    missing = [f for f in _REQUIRED_BILLING_ADDRESS_FIELDS
               if not (row[f] or "").strip()]
    if missing:
        # Strip the `billing_` prefix in the error message to match the API
        # field names the caller would use to PATCH /v1/tenants/{tid}/billing-address.
        api_names = [f.removeprefix("billing_") for f in missing]
        raise ValueError(
            f"provision_subscription: customer tenant '{row['tenant_id']}' "
            f"is missing required billing-address fields: {api_names}. "
            f"PATCH /v1/tenants/{{tenant_id}}/billing-address before provisioning."
        )


async def provision_subscription(
    user_id: str,
    plan_id: str,
    seats: int,
) -> Dict[str, Any]:
    """
    Directly provision an active subscription without Mollie — for seeding.

    Produces exactly the state that a successful Mollie checkout + webhook cycle
    would produce (status='active', monthly budget entry set) without initiating
    a payment. Synthetic placeholder values fill the Mollie-specific columns so
    callers without a real Mollie customer can still own a valid subscription row.

    Idempotent: if the user already has an active subscription for this plan,
    returns it without creating a duplicate. Refuses trial plans (use the normal
    checkout flow for those — trial provisioning happens auto in evaluate_budget).

    Fail-fast: refuses to provision if the user's tenant has no complete
    billing-address (§11 UStG — invoices require a recipient address). Seeders
    must populate the address before provisioning; FE checkout flows must
    require it during onboarding.

    The synthetic mollie_first_payment_id ('provision-{user_id}-{plan_id}') acts
    as the idempotency key for concurrent callers, just like the real payment ID
    does in _activate_subscription.
    """
    from src.budget.plans import get_plan

    plan = get_plan(plan_id)  # raises ValueError for unknown plan
    if plan.trial:
        raise ValueError(
            f"provision_subscription: cannot provision trial plan '{plan_id}' — "
            "trials are auto-provisioned via evaluate_budget"
        )

    pool = get_pool()
    user_uuid = uuid.UUID(user_id)

    # Fail-fast pre-check: a customer subscription without billing-address
    # yields invoices that cannot legally be issued (§11 UStG). Refuse here
    # so the incomplete state never exists, rather than discovering it
    # later when auto_create_invoice silently marks the invoice 'incomplete'.
    # Trial plans + test/internal tenants are exempt (see helper docstring).
    async with pool.acquire() as conn:
        await _assert_complete_billing_address(conn, user_uuid, plan_id)

    # Pre-check: if the user already owns an active subscription for this plan,
    # return it immediately. This covers the common idempotent re-seed case
    # without going through the INSERT path.
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT id, user_id, app_id, plan_id, status, mollie_customer_id,
                   mollie_subscription_id, seats, started_at, cancelled_at,
                   suspended_at, expired_at
            FROM subscriptions
            WHERE user_id = $1 AND plan_id = $2::plan_id AND status = 'active'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            user_uuid, plan_id,
        )
        if existing:
            return _serialize_subscription(existing)

        # No active subscription yet. Insert with synthetic Mollie identifiers.
        # mollie_first_payment_id is the UNIQUE idempotency key — concurrent callers
        # that both passed the pre-check will race here; one wins, one hits ON CONFLICT.
        synth_payment_id = f"provision-{user_id}-{plan_id}"
        synth_customer_id = f"seed-{user_id}"
        sub_uuid = uuid.uuid4()

        row = await conn.fetchrow(
            """
            INSERT INTO subscriptions
              (id, user_id, app_id, plan_id, status,
               mollie_customer_id, mollie_subscription_id, mollie_first_payment_id,
               seats, started_at, metadata)
            VALUES ($1, $2, $3, $4::plan_id, 'active',
                    $5, NULL, $6, $7, NOW(), '{"provisioned": true}'::jsonb)
            ON CONFLICT (mollie_first_payment_id) DO NOTHING
            RETURNING id, user_id, app_id, plan_id, status, mollie_customer_id,
                      mollie_subscription_id, seats, started_at, cancelled_at,
                      suspended_at, expired_at
            """,
            sub_uuid, user_uuid, plan.app_id, plan_id,
            synth_customer_id, synth_payment_id, seats,
        )

        if row:
            await _provision_plan_budget(conn, user_uuid, plan)
            sub_dict = _serialize_subscription(row)
            sub_id = sub_dict["id"]
        else:
            # ON CONFLICT: concurrent caller won the race. Return their row.
            winner = await conn.fetchrow(
                """
                SELECT id, user_id, app_id, plan_id, status, mollie_customer_id,
                       mollie_subscription_id, seats, started_at, cancelled_at,
                       suspended_at, expired_at
                FROM subscriptions WHERE mollie_first_payment_id = $1
                """,
                synth_payment_id,
            )
            if not winner:
                raise RuntimeError(
                    f"provision_subscription: ON CONFLICT fired but no row found "
                    f"for payment_id={synth_payment_id}"
                )
            sub_dict = _serialize_subscription(winner)
            sub_id = sub_dict["id"]

    await log_billing_event(
        "subscription.provisioned",
        user_id=user_id,
        subscription_id=sub_id,
        source="seed",
        payload={"planId": plan_id, "seats": seats},
    )
    return sub_dict


# ---------------------------------------------------------------------------
# Auto-invoice helper — called from Mollie-webhook handlers after a paid event.
# ---------------------------------------------------------------------------

async def auto_create_invoice(
    *,
    user_id: str,
    amount_eur: float,
    description: str,
    subscription_id: str | None = None,
    credit_purchase_id: str | None = None,
    mollie_payment_id: str | None = None,
) -> str | None:
    """Insert one invoice row, mark it 'paid', return the id.

    Idempotent: if an invoice already exists for this mollie_payment_id, return
    the existing id without re-inserting (fixes Mollie webhook retries).

    Resolves the tenant's billing address to populate invoices.billing_address
    and determine the correct tax rate (AT: 20%, EU B2B Reverse Charge: 0%,
    non-EU export: 0%).  If the address is missing the invoice is still created
    — the payment happened and the record MUST exist — but metadata carries
    {"incomplete": True, "missingBillingAddress": True} so operators can
    identify and correct the gap.

    EU B2C tax (OSS-Verfahren) is an OPEN DECISION: see _determine_tax_rate.
    """
    import json as _json
    from decimal import Decimal
    from datetime import datetime, timezone

    pool = get_pool()
    if mollie_payment_id:
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT id FROM invoices WHERE mollie_payment_id = $1",
                mollie_payment_id,
            )
        if existing:
            return str(existing)

    # Resolve tenant and billing address.  Failure is non-fatal: invoice is
    # created with NULL billing_address and marked incomplete.
    tenant_id: str | None = None
    billing_address: Dict[str, Any] | None = None
    eu_b2c_flag = False
    try:
        async with pool.acquire() as conn:
            trow = await conn.fetchrow(
                """
                SELECT u.tenant_id,
                       t.billing_name, t.billing_street, t.billing_city,
                       t.billing_postcode, t.billing_country, t.billing_vat_id
                FROM users u
                LEFT JOIN tenants t ON t.id = u.tenant_id
                WHERE u.id = $1
                """,
                uuid.UUID(user_id),
            )
        if trow and trow["tenant_id"]:
            tenant_id = trow["tenant_id"]
            if trow["billing_name"]:
                billing_address = {
                    "name": trow["billing_name"],
                    "street": trow["billing_street"],
                    "city": trow["billing_city"],
                    "postcode": trow["billing_postcode"],
                    "country": (trow["billing_country"] or "").upper().strip() or None,
                    "vatId": trow["billing_vat_id"],
                }
    except Exception:
        pass  # billing_address stays None — marked incomplete below

    tax_rate, reverse_charge_note = _determine_tax_rate(billing_address)

    # Detect EU B2C (no vatId in EU non-AT country) — mark for operator review.
    if billing_address:
        country = (billing_address.get("country") or "").upper()
        vat_id = (billing_address.get("vatId") or "").strip()
        if country in _EU_COUNTRIES_NON_AT and not vat_id:
            eu_b2c_flag = True

    subtotal = Decimal(str(amount_eur))
    tax = (subtotal * Decimal(str(tax_rate)) / Decimal("100")).quantize(Decimal("0.01"))
    total = (subtotal + tax).quantize(Decimal("0.01"))

    line_items = [{
        "description": description,
        "quantity": 1,
        "unitPriceEur": float(subtotal),
        "totalEur": float(subtotal),
        "metadata": {},
    }]

    metadata: Dict[str, Any] = {"autoCreated": True, "source": "mollie-webhook"}
    if billing_address is None:
        metadata["incomplete"] = True
        metadata["missingBillingAddress"] = True
    if reverse_charge_note:
        metadata["reverseChargeNote"] = reverse_charge_note
    if eu_b2c_flag:
        # EU B2C without vatId — OSS-Verfahren applies, tax rate is approximate.
        # OPEN DECISION: must be reviewed with tax advisor before serving EU B2C volume.
        metadata["taxNote"] = "EU_B2C_OSS_OPEN"

    now = datetime.now(timezone.utc)
    year = now.year
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"CREATE SEQUENCE IF NOT EXISTS invoice_seq_{year} START 1 INCREMENT 1")
            seq_n = await conn.fetchval(f"SELECT nextval('invoice_seq_{year}')")
            invoice_number = f"INV-{year}-{int(seq_n):05d}"

            row = await conn.fetchrow(
                """
                INSERT INTO invoices
                  (invoice_number, user_id, tenant_id, subscription_id, credit_purchase_id,
                   mollie_payment_id, status, subtotal_eur, tax_rate, tax_eur, total_eur,
                   currency, line_items, billing_address, issued_at, paid_at, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, 'paid', $7, $8, $9, $10, 'EUR', $11::jsonb,
                        $12::jsonb, NOW(), NOW(), $13::jsonb)
                ON CONFLICT (mollie_payment_id) DO NOTHING
                RETURNING id
                """,
                invoice_number,
                uuid.UUID(user_id),
                tenant_id,
                uuid.UUID(subscription_id) if subscription_id else None,
                uuid.UUID(credit_purchase_id) if credit_purchase_id else None,
                mollie_payment_id,
                subtotal, tax_rate, tax, total,
                _json.dumps(line_items),
                _json.dumps(billing_address) if billing_address else None,
                _json.dumps(metadata),
            )

    if row:
        invoice_id = str(row["id"])
        await log_billing_event(
            "invoice.issued",
            user_id=user_id,
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            invoice_id=invoice_id,
            mollie_payment_id=mollie_payment_id,
            amount_eur=float(total),
            source="mollie-webhook",
            payload={"invoiceNumber": invoice_number, "auto": True},
        )
        return invoice_id
    # ON CONFLICT means another concurrent call won — find their id.
    if mollie_payment_id:
        async with pool.acquire() as conn:
            return str(await conn.fetchval(
                "SELECT id FROM invoices WHERE mollie_payment_id = $1",
                mollie_payment_id,
            ))
    return None


async def seed_legacy_trials(app_id: str) -> Dict[str, int]:
    """
    Backfill: for every user without an active subscription for `app_id`,
    insert a 7-day trial subscription + app_license. Idempotent.

    Mirrors the trial-seeding path in identity/routes.py register() so legacy
    users (created before the trial-auto-seeding landed) get the same grace
    window. Returns counts so callers can verify scope before tightening the
    budget gate to block `unlicensed`.

    trial_ends_at = NOW + 7 days. start_date = today. Same rules as register.
    """
    pool = get_pool()
    now = datetime.now(timezone.utc)
    today = now.date()
    trial_ends = now + timedelta(days=7)

    created = 0
    skipped = 0

    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT id FROM users")
        for u in users:
            uid = u["id"]
            existing = await conn.fetchval(
                """
                SELECT 1 FROM subscriptions
                WHERE user_id = $1 AND app_id = $2::app_id AND status = 'active'
                LIMIT 1
                """,
                uid, app_id,
            )
            if existing:
                skipped += 1
                continue

            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO app_licenses (user_id, app_id, plan_id, start_date, end_date, seats)
                    VALUES ($1, $2::app_id, 'trial'::plan_id, $3, NULL, 1)
                    ON CONFLICT DO NOTHING
                    """,
                    uid, app_id, today,
                )
                await conn.execute(
                    """
                    INSERT INTO subscriptions
                        (user_id, app_id, plan_id, status, mollie_customer_id,
                         seats, started_at, trial_ends_at)
                    VALUES
                        ($1, $2::app_id, 'trial'::plan_id, 'active'::subscription_status,
                         NULL, 1, $3, $4)
                    """,
                    uid, app_id, now, trial_ends,
                )
            created += 1

    return {"app_id": app_id, "created": created, "skipped": skipped, "total": len(users)}

