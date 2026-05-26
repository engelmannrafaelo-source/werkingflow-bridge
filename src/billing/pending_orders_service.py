"""
PendingOrdersService — Rechnungs-Lane (Variante A: manuelle Freigabe).

Workflow:
  create_pending_order  → erzeugt Invoice (issued) + pending_orders-Row + Email
  release_order         → Operator gibt frei: Subscription aktivieren + Invoice paid

Subscription-Plans (interval='month'):
  → subscriptions-Row mit synthetic Mollie-IDs (kein Mollie-Recurring).

Project-Plans (interval='project'):
  → OPEN DECISION — nicht implementiert, wirft NotImplementedError.
  Siehe Memory-Memo project_pending_orders_project_plan_open_decision.md.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx

from src.api_auth.tenant_resolver import resolve_tenant_for_user
from src.billing.billing_service import (
    _determine_tax_rate,
    _provision_plan_budget,
    log_billing_event,
)
from src.db.client import get_pool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _serialize_order(row: Any) -> Dict[str, Any]:
    def _ts(col: str) -> Optional[str]:
        v = row[col] if col in row.keys() else None
        return v.isoformat() if v else None

    return {
        "id": str(row["id"]),
        "userId": str(row["user_id"]),
        "tenantId": row["tenant_id"],
        "planId": row["plan_id"],
        "quantity": row["quantity"],
        "totalPriceEur": str(row["total_price_eur"]),
        "status": row["status"],
        "invoiceId": str(row["invoice_id"]),
        "createdAt": _ts("created_at"),
        "releasedAt": _ts("released_at"),
        "releasedBy": str(row["released_by"]) if row["released_by"] else None,
        "releaseNote": row["release_note"],
    }


async def _next_invoice_number(conn: Any) -> str:
    """Per-Jahr-Sequenz: INV-<year>-<5-digit-seq>. Identisch zu invoices/routes.py."""
    year = datetime.now(timezone.utc).year
    seq_name = f"invoice_seq_{year}"
    await conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq_name} START 1 INCREMENT 1")
    n = await conn.fetchval(f"SELECT nextval('{seq_name}')")
    return f"INV-{year}-{int(n):05d}"


async def _create_order_invoice(
    user_id: str,
    plan_name: str,
    plan_id: str,
    quantity: int,
    unit_price_eur: float,
    total_eur: float,
) -> str:
    """
    Erstellt eine Invoice mit status='issued' für eine Pending-Order.

    Verwendet dieselbe Billing-Address-Auflösung wie auto_create_invoice,
    aber status='issued' (nicht 'paid') — Zahlung noch ausstehend.
    due_at = 14 Tage ab Ausstellungsdatum (DACH B2B Standard Vorkasse).
    """
    pool = get_pool()

    tenant_id: Optional[str] = None
    billing_address: Optional[Dict[str, Any]] = None
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
        pass

    tax_rate, reverse_charge_note = _determine_tax_rate(billing_address)

    from src.billing.billing_service import _EU_COUNTRIES_NON_AT
    if billing_address:
        country = (billing_address.get("country") or "").upper()
        vat_id = (billing_address.get("vatId") or "").strip()
        if country in _EU_COUNTRIES_NON_AT and not vat_id:
            eu_b2c_flag = True

    subtotal = Decimal(str(total_eur))
    tax = (subtotal * Decimal(str(tax_rate)) / Decimal("100")).quantize(Decimal("0.01"))
    total = (subtotal + tax).quantize(Decimal("0.01"))

    line_items = [{
        "description": f"{plan_name} ({plan_id})" + (f" × {quantity}" if quantity > 1 else ""),
        "quantity": quantity,
        "unitPriceEur": unit_price_eur,
        "totalEur": float(subtotal),
        "metadata": {},
    }]

    metadata: Dict[str, Any] = {"manualBilling": True, "pendingOrder": True}
    if billing_address is None:
        metadata["incomplete"] = True
        metadata["missingBillingAddress"] = True
    if reverse_charge_note:
        metadata["reverseChargeNote"] = reverse_charge_note
    if eu_b2c_flag:
        metadata["taxNote"] = "EU_B2C_OSS_OPEN"

    now = datetime.now(timezone.utc)
    due_at = now + timedelta(days=14)

    async with pool.acquire() as conn:
        async with conn.transaction():
            invoice_number = await _next_invoice_number(conn)
            row = await conn.fetchrow(
                """
                INSERT INTO invoices
                  (invoice_number, user_id, tenant_id, status,
                   subtotal_eur, tax_rate, tax_eur, total_eur, currency,
                   line_items, billing_address, issued_at, due_at, metadata)
                VALUES ($1, $2, $3, 'issued',
                        $4, $5, $6, $7, 'EUR',
                        $8::jsonb, $9::jsonb, NOW(), $10, $11::jsonb)
                RETURNING id
                """,
                invoice_number,
                uuid.UUID(user_id),
                tenant_id,
                subtotal, tax_rate, tax, total,
                json.dumps(line_items),
                json.dumps(billing_address) if billing_address else None,
                due_at,
                json.dumps(metadata),
            )

    invoice_id = str(row["id"])
    await log_billing_event(
        "invoice.issued",
        user_id=user_id,
        tenant_id=tenant_id,
        invoice_id=invoice_id,
        amount_eur=float(total),
        source="pending-order",
        payload={"invoiceNumber": invoice_number, "planId": plan_id, "quantity": quantity},
    )
    return invoice_id


async def _send_order_email(invoice_id: str, user_id: str) -> None:
    """
    Versendet die Rechnung per Email via Resend.

    Fail-fast wenn RESEND_API_KEY nicht konfiguriert — fehlende Email-Konfiguration
    darf nie silent fail. Der Caller (create_pending_order) fängt RuntimeError
    und gibt 502 zurück.
    """
    resend_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("RESEND_INVOICE_SENDER", "billing@werking.tools")
    if not resend_key:
        raise RuntimeError("RESEND_API_KEY not configured on bridge — cannot send order email")

    pool = get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT i.*, u.email AS user_email FROM invoices i "
            "JOIN users u ON u.id = i.user_id WHERE i.id = $1",
            uuid.UUID(invoice_id),
        )
    if not r:
        raise RuntimeError(f"Invoice {invoice_id} not found for email send")
    recipient = r["user_email"]
    if not recipient:
        raise RuntimeError(f"User {user_id} has no email — cannot send order invoice")

    # Render HTML inline (reuse the render function from invoices/routes.py)
    from src.invoices.routes import _row as _inv_row, _render_html
    inv_dict = _inv_row(r)
    html_body = _render_html(inv_dict)
    subject = f"Rechnung {inv_dict['invoiceNumber']} — Bestellung ausstehend"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "html": html_body,
            },
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Resend error {resp.status_code}: {resp.text[:200]}"
        )

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE invoices SET sent_at = COALESCE(sent_at, NOW()), updated_at = NOW() WHERE id = $1",
            uuid.UUID(invoice_id),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create_pending_order(
    user_id: str,
    plan_id: str,
    quantity: int = 1,
) -> Dict[str, Any]:
    """
    Erzeugt eine Pending-Order: Invoice generieren, Row schreiben, Email senden.

    Raises:
      ValueError   — unbekannter oder Trial-Plan, quantity < 1
      RuntimeError — Email-Versand fehlgeschlagen (RESEND_API_KEY fehlt o.ä.)
    """
    from src.budget.plans import get_plan
    plan = get_plan(plan_id)
    if plan.trial:
        raise ValueError(f"Cannot create pending order for trial plan '{plan_id}'")
    if quantity < 1:
        raise ValueError("quantity must be >= 1")

    total_eur = round(plan.price * quantity, 2)
    tenant_id = await resolve_tenant_for_user(user_id)

    invoice_id = await _create_order_invoice(
        user_id=user_id,
        plan_name=plan.name,
        plan_id=plan_id,
        quantity=quantity,
        unit_price_eur=float(plan.price),
        total_eur=total_eur,
    )

    pool = get_pool()
    order_id = uuid.uuid4()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO pending_orders
              (id, user_id, tenant_id, plan_id, quantity, total_price_eur, status, invoice_id)
            VALUES ($1, $2, $3, $4, $5, $6, 'awaiting_payment', $7)
            RETURNING *
            """,
            order_id,
            uuid.UUID(user_id),
            tenant_id,
            plan_id,
            quantity,
            Decimal(str(total_eur)),
            uuid.UUID(invoice_id),
        )

    await log_billing_event(
        "order.created",
        user_id=user_id,
        tenant_id=tenant_id,
        invoice_id=invoice_id,
        amount_eur=total_eur,
        source="customer",
        payload={"planId": plan_id, "quantity": quantity, "orderId": str(order_id)},
    )

    await _send_order_email(invoice_id=invoice_id, user_id=user_id)

    return _serialize_order(row)


async def list_user_pending_orders(user_id: str) -> List[Dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM pending_orders WHERE user_id = $1 ORDER BY created_at DESC",
            uuid.UUID(user_id),
        )
    return [_serialize_order(r) for r in rows]


async def list_all_pending_orders(
    status_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    pool = get_pool()
    if status_filter:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM pending_orders WHERE status = $1 ORDER BY created_at DESC",
                status_filter,
            )
    else:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM pending_orders ORDER BY created_at DESC"
            )
    return [_serialize_order(r) for r in rows]


async def release_order(
    order_id: str,
    operator_user_id: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Gibt eine Pending-Order frei: Subscription aktivieren + Invoice auf 'paid' setzen.

    Subscription-Plans (interval='month'):
      → subscriptions-Row mit status='active' (synthetic Mollie-IDs, kein Recurring).
      → user_budgets-Eintrag provisionieren (30-Tage-Fenster).

    Project-Plans (interval='project'):
      → NICHT implementiert. Offene Entscheidung:
        Variante 1: user_topup_balances (API-Credits)
        Variante 2: neue Tabelle manual_project_credits (semantisch sauberer für Projekt-Slots)
        Semantik unklar bis Bridge-Energy-Integration vollständig bekannt ist.
        Siehe Memory-Memo: project_pending_orders_project_plan_open_decision.md

    Raises:
      LookupError   — Order nicht gefunden
      ValueError    — Order nicht im Status 'awaiting_payment' (bereits released/expired/cancelled)
      NotImplementedError — Project-Plan-Release noch nicht implementiert
    """
    from src.budget.plans import get_plan

    pool = get_pool()
    order_uuid = uuid.UUID(order_id)
    operator_uuid = uuid.UUID(operator_user_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            order = await conn.fetchrow(
                "SELECT * FROM pending_orders WHERE id = $1 FOR UPDATE",
                order_uuid,
            )
            if not order:
                raise LookupError(f"Pending order {order_id} not found")
            if order["status"] != "awaiting_payment":
                raise ValueError(
                    f"Order {order_id} is '{order['status']}', "
                    "only 'awaiting_payment' orders can be released"
                )

            plan = get_plan(order["plan_id"])

            if plan.interval == "project":
                # OPEN DECISION — nicht implementiert.
                # Semantik von "Projekt-Credit freigeben" unklar:
                #   Variante 1: user_topup_balances (EUR-Budget als Proxy)
                #   Variante 2: manual_project_credits Tabelle (Slot-Count)
                # Bridge-Energy-Integration muss zuerst klären wie Slots gezählt werden.
                raise NotImplementedError(
                    f"release_order for project-plan '{plan.interval}' is not implemented. "
                    "Open decision: see memory memo project_pending_orders_project_plan_open_decision.md"
                )

            if plan.interval == "month":
                # Synthetic IDs — kein Mollie-Recurring für manuelle Billing-Lane.
                synth_payment_id = f"manual-order-{order_id}"
                synth_customer_id = "manual-billing"
                sub_uuid = uuid.uuid4()
                user_uuid = order["user_id"]

                sub_row = await conn.fetchrow(
                    """
                    INSERT INTO subscriptions
                      (id, user_id, app_id, plan_id, status,
                       mollie_customer_id, mollie_subscription_id, mollie_first_payment_id,
                       seats, started_at, metadata)
                    VALUES ($1, $2, $3, $4::plan_id, 'active',
                            $5, NULL, $6, $7, NOW(),
                            '{"manualBilling": true}'::jsonb)
                    ON CONFLICT (mollie_first_payment_id) DO NOTHING
                    RETURNING id, user_id, app_id, plan_id, status, mollie_customer_id,
                              mollie_subscription_id, seats, started_at, cancelled_at,
                              suspended_at, expired_at
                    """,
                    sub_uuid,
                    user_uuid,
                    plan.app_id,
                    plan.id,
                    synth_customer_id,
                    synth_payment_id,
                    order["quantity"],
                )

                if sub_row:
                    await _provision_plan_budget(conn, user_uuid, plan)
                    sub_id = str(sub_row["id"])
                else:
                    # Concurrent release — row already exists.
                    existing_sub = await conn.fetchrow(
                        "SELECT id FROM subscriptions WHERE mollie_first_payment_id = $1",
                        synth_payment_id,
                    )
                    sub_id = str(existing_sub["id"]) if existing_sub else None
            else:
                raise ValueError(
                    f"Unknown plan interval '{plan.interval}' for plan '{plan.id}' — "
                    "cannot release order"
                )

            # Mark invoice as paid
            await conn.execute(
                """
                UPDATE invoices
                   SET status = 'paid', paid_at = COALESCE(paid_at, NOW()), updated_at = NOW()
                 WHERE id = $1
                """,
                order["invoice_id"],
            )

            # Release the order
            released_row = await conn.fetchrow(
                """
                UPDATE pending_orders
                   SET status = 'released',
                       released_at = NOW(),
                       released_by = $2,
                       release_note = $3
                 WHERE id = $1
                RETURNING *
                """,
                order_uuid,
                operator_uuid,
                note,
            )

    await log_billing_event(
        "order.released",
        user_id=str(order["user_id"]),
        invoice_id=str(order["invoice_id"]),
        amount_eur=float(order["total_price_eur"]),
        source="operator",
        payload={
            "orderId": order_id,
            "planId": order["plan_id"],
            "quantity": order["quantity"],
            "subscriptionId": sub_id if plan.interval == "month" else None,
            "releasedBy": operator_user_id,
            "releaseNote": note,
        },
    )

    return _serialize_order(released_row)
