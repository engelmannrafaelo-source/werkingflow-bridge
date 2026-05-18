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

